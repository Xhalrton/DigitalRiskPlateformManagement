import os
import re
import json
import hmac
import hashlib
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from groq import Groq
from supabase import create_client

app = Flask(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

GROQ_KEY      = os.environ.get("GROQ_KEY")
WA_TOKEN      = os.environ.get("WA_TOKEN", "placeholder")
WA_PHONE_ID   = os.environ.get("WA_PHONE_ID", "placeholder")
WA_APP_SECRET = os.environ.get("WA_APP_SECRET", "placeholder")
SUPABASE_URL  = os.environ.get("SUPABASE_URL")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY")

# Contacts de test
CONTACTS = {
    "CHEF_PROJET": "+2250101089251",
    "PMO":         "+2250555444241",
    "DIRECTEUR":   "+2250506574905"
}

ESCALADE = {
    "FAIBLE":   [],
    "MOYEN":    ["CHEF_PROJET"],
    "ELEVE":    ["CHEF_PROJET", "PMO"],
    "CRITIQUE": ["CHEF_PROJET", "PMO", "DIRECTEUR"]
}

TYPES_PROJET = ["RAN", "RURAL", "FIBRE", "CORE", "IPRAN", "MWV", "MMONEY", "HOME", "AUTRES"]
TYPES_RISQUE = ["ACCES", "SECURITE", "TECHNIQUE", "ADMINISTRATIF", "METEO", "LOGISTIQUE", "SANITAIRE", "SOCIAL", "AUTRES"]

messages_traites = set()

# Clients
groq_client = Groq(api_key=GROQ_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

LIEN_DASHBOARD = os.environ.get("DASHBOARD_URL", "https://google.com")

# ============================================================
# ETATS DE CONVERSATION
# ============================================================

conversations = {}

"""
ETAPES:
0: Attente "#"
1: Attente type_projet
2: Attente nom_projet
3: Attente site
4: Attente description risque
5: Attente confirmation
"""

# ============================================================
# SECURITE
# ============================================================

def verifier_signature(request):
    if not WA_APP_SECRET or WA_APP_SECRET == "placeholder":
        return True
    signature = request.headers.get('X-Hub-Signature-256', '')
    expected = hmac.new(
        WA_APP_SECRET.encode(),
        request.get_data(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

# ============================================================
# GENERATION ID : XXXAAMM0000
# ============================================================

def get_prefix(type_projet):
    """Récupère le prefix personnalisé depuis Supabase ou défaut"""
    if not supabase:
        return type_projet[:3].upper()
    try:
        result = supabase.table("config_prefixes").select("prefix").eq("type_projet", type_projet).execute()
        if result.data:
            return result.data[0]["prefix"]
    except Exception as e:
        print(f"==> Erreur prefix: {e}")
    return type_projet[:3].upper()

def generer_risque_id(type_projet):
    """
    Génère ID format XXXAAMM0000
    3 caractères prefix + 2 année + 2 mois + 4 séquence
    Ex: RAN26050001, FIB26050142
    """
    prefix = get_prefix(type_projet)
    now = datetime.now()
    aa = str(now.year)[-2:]
    mm = f"{now.month:02d}"
    base_prefix = f"{prefix}{aa}{mm}"
    
    if supabase:
        try:
            result = supabase.table("risques").select("risque_id").like("risque_id", f"{base_prefix}%").execute()
            count = len(result.data) if result.data else 0
        except Exception as e:
            print(f"==> Erreur comptage: {e}")
            count = 0
    else:
        count = 0
    
    sequence = count + 1
    return f"{base_prefix}{sequence:04d}"

# ============================================================
# SUPABASE
# ============================================================

def sauvegarder_risque_complet(data, message_id=None):
    if not supabase:
        return None
    try:
        data["message_id_whatsapp"] = message_id
        result = supabase.table("risques").insert(data).execute()
        return result.data[0]["risque_id"] if result.data else None
    except Exception as e:
        print(f"==> Supabase erreur: {e}")
        return None

def recuperer_risque_par_id(risque_id):
    if not supabase:
        return None
    try:
        result = supabase.table("risques").select("*").eq("risque_id", risque_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        print(f"==> Erreur recup: {e}")
        return None

def mettre_a_jour_risque(risque_id, updates):
    if not supabase:
        return False
    try:
        supabase.table("risques").update(updates).eq("risque_id", risque_id).execute()
        return True
    except Exception as e:
        print(f"==> Erreur MAJ: {e}")
        return False

def fermer_risque(risque_id, reponse, par_qui):
    return mettre_a_jour_risque(risque_id, {
        "statut": "CLOSED",
        "reponse_apportee": reponse,
        "owner_contact": par_qui
    })

def get_astreinte(role):
    if not supabase:
        return {"telephone": CONTACTS.get(role), "nom": role}
    try:
        result = supabase.table("astreintes").select("utilisateurs(telephone, nom, prenom)").eq(
            "role", role
        ).eq("actif", True).lte("date_debut", datetime.now().isoformat()).gte(
            "date_fin", datetime.now().isoformat()
        ).execute()
        if result.data and result.data[0].get("utilisateurs"):
            u = result.data[0]["utilisateurs"]
            return {
                "telephone": u["telephone"],
                "nom": f"{u.get('prenom', '')} {u['nom']}".strip()
            }
    except Exception as e:
        print(f"==> Erreur astreinte: {e}")
    return {"telephone": CONTACTS.get(role), "nom": role}

# ============================================================
# WHATSAPP
# ============================================================

def envoyer_whatsapp(numero, message, message_id_reference=None):
    if WA_TOKEN == "placeholder":
        print(f"==> [SIMULATION] WhatsApp à {numero}: {message[:50]}...")
        return None
    
    url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": message}
    }
    if message_id_reference:
        data["context"] = {"message_id": message_id_reference}
    
    try:
        resp = requests.post(url, headers=headers, json=data)
        print(f"==> WhatsApp à {numero}: {resp.status_code}")
        return resp.json().get("messages", [{}])[0].get("id")
    except Exception as e:
        print(f"==> Erreur envoi: {e}")
        return None

# ============================================================
# IA
# ============================================================

def analyser_risque_ia(description, site, type_projet):
    prompt = f"""Tu es un expert en gestion des risques telecoms.

CONTEXTE:
- Projet: {type_projet}
- Site: {site}
- Description: "{description}"
- Date: {datetime.now().isoformat()}

Evalue ce risque et reponds UNIQUEMENT en JSON:

{{
  "type_risque": "ACCES|SECURITE|TECHNIQUE|ADMINISTRATIF|METEO|LOGISTIQUE|SANITAIRE|SOCIAL|AUTRES",
  "description_risque": "synthese claire",
  "impact_risque": "impact concret sur operations",
  "score_probabilite_1_5": 1-5,
  "score_impact_1_5": 1-5,
  "bloquer_projet": true|false,
  "strategie_de_reponse": "EVITEMENT|REDUCTION|TRANSFERT|ACCEPTATION",
  "action_immediate": "action concrete pour le manager",
  "confiance": 0.0-1.0,
  "justification_scores": "15 mots max"
}}

REGLES SCORING:
- Probabilite: 1=quasi impossible, 2=peu probable, 3=possible, 4=probable, 5=quasi certain
- Impact: 1=negligeable, 2=mineur, 3=modere, 4=majeur, 5=catastrophique

Ne mets rien d'autre que le JSON."""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=600
        )
        result = json.loads(response.choices[0].message.content)
        
        if result.get("type_risque") not in TYPES_RISQUE:
            result["type_risque"] = "AUTRES"
        
        prob = result.get("score_probabilite_1_5")
        imp = result.get("score_impact_1_5")
        
        if not isinstance(prob, int) or not (1 <= prob <= 5):
            result["score_probabilite_1_5"] = 3
        if not isinstance(imp, int) or not (1 <= imp <= 5):
            result["score_impact_1_5"] = 3
        if not isinstance(result.get("confiance"), (int, float)):
            result["confiance"] = 0.5
            
        return result
        
    except Exception as e:
        print(f"==> Erreur IA: {e}")
        return {
            "type_risque": "AUTRES",
            "description_risque": description[:100],
            "impact_risque": "A evaluer",
            "score_probabilite_1_5": 3,
            "score_impact_1_5": 3,
            "bloquer_projet": False,
            "strategie_de_reponse": "REDUCTION",
            "action_immediate": "Evaluation sur site requise",
            "confiance": 0.3,
            "justification_scores": "Erreur IA - defaut"
        }

# ============================================================
# CONVERSATION INTERACTIVE
# ============================================================

def envoyer_formulaire_etape(numero, etape, data=None):
    messages = {
        0: (
            "DigitalRiskPlatform\n\n"
            "Pour signaler un risque, envoyez un message commencant par #\n\n"
            "Exemple: # je veux signaler un risque\n\n"
            "Ou envoyez simplement # pour commencer."
        ),
        1: (
            "Etape 1/4 - Type de projet:\n\n"
            "1. RAN\n"
            "2. RURAL\n"
            "3. FIBRE\n"
            "4. CORE\n"
            "5. IPRAN\n"
            "6. MWV\n"
            "7. MMONEY\n"
            "8. HOME\n"
            "9. AUTRES\n\n"
            "Repondez par le numero ou le nom."
        ),
        2: (
            "Etape 2/4 - Nom du projet:\n\n"
            "Indiquez le nom precis du projet.\n"
            "Exemple: Deploiement RAN Abidjan Nord\n\n"
            "Ce nom pourra etre modifie par l'administrateur."
        ),
        3: (
            "Etape 3/4 - Site concerne:\n\n"
            "Indiquez le nom ou code du site.\n"
            "Exemple: Cocody, ABJ_COC_001"
        ),
        4: (
            "Etape 4/4 - Description du risque:\n\n"
            "Decrivez le risque:\n"
            "- Quel est le probleme?\n"
            "- Quand a-t-il commence?\n"
            "- Personnes en danger?\n"
            "- Travail bloque?\n\n"
            "Soyez precis, l'IA analysera votre message."
        ),
        5: None
    }
    
    if etape == 5 and data:
        msg = (
            f"Resume de votre signalement:\n\n"
            f"Projet: {data.get('type_projet', '?')}\n"
            f"Nom: {data.get('nom_projet', '?')}\n"
            f"Site: {data.get('site', '?')}\n"
            f"Description: {data.get('description', '?')[:100]}...\n\n"
            f"Envoyez OK pour valider et analyser,\n"
            f"ou ANNULER pour recommencer."
        )
    else:
        msg = messages.get(etape, "Etape inconnue.")
    
    envoyer_whatsapp(numero, msg)

def traiter_etape_conversation(expediteur, message, message_id):
    msg_upper = message.strip().upper()
    
    # Nouveau signalement
    if expediteur not in conversations:
        if not msg_upper.startswith("#"):
            envoyer_whatsapp(expediteur, 
                "Pour signaler un risque, votre message doit commencer par #\n\n"
                "Envoyez # pour commencer.")
            return True
        
        conversations[expediteur] = {"etape": 1, "data": {}}
        envoyer_formulaire_etape(expediteur, 1)
        return True
    
    conv = conversations[expediteur]
    etape = conv["etape"]
    data = conv["data"]
    
    # Etape 1: Type projet
    if etape == 1:
        type_proj = None
        for i, tp in enumerate(TYPES_PROJET, 1):
            if str(i) == msg_upper or tp == msg_upper:
                type_proj = tp
                break
        
        if not type_proj:
            envoyer_whatsapp(expediteur, 
                "Non reconnu. Choisissez: RAN, RURAL, FIBRE, CORE, IPRAN, MWV, MMONEY, HOME, AUTRES")
            return True
        
        data["type_projet"] = type_proj
        conv["etape"] = 2
        envoyer_formulaire_etape(expediteur, 2)
        return True
    
    # Etape 2: Nom projet
    elif etape == 2:
        data["nom_projet"] = message.strip()
        conv["etape"] = 3
        envoyer_formulaire_etape(expediteur, 3)
        return True
    
    # Etape 3: Site
    elif etape == 3:
        data["site"] = message.strip()
        conv["etape"] = 4
        envoyer_formulaire_etape(expediteur, 4)
        return True
    
    # Etape 4: Description
    elif etape == 4:
        data["description"] = message.strip()
        conv["etape"] = 5
        envoyer_formulaire_etape(expediteur, 5, data)
        return True
    
    # Etape 5: Confirmation
    elif etape == 5:
        if msg_upper in ["OK", "OUI", "VALIDER"]:
            return traiter_risque_confirme(expediteur, data, message_id)
        elif msg_upper in ["ANNULER", "NON", "RESET"]:
            del conversations[expediteur]
            envoyer_whatsapp(expediteur, "Annule. Envoyez # pour recommencer.")
            return True
        else:
            envoyer_whatsapp(expediteur, "Repondez OK ou ANNULER.")
            return True
    
    return False

def traiter_risque_confirme(expediteur, data, message_id):
    envoyer_whatsapp(expediteur, "Analyse IA en cours... Patientez.")
    
    analyse = analyser_risque_ia(data["description"], data["site"], data["type_projet"])
    
    # Générer ID XXXAAMM0000
    risque_id = generer_risque_id(data["type_projet"])
    
    db_data = {
        "risque_id": risque_id,
        "source_stakeholder_contact": expediteur,
        "type_projet": data["type_projet"],
        "nom_projet": data["nom_projet"],
        "site": data["site"],
        "message_original": f"# {data['description']}",
        "type_risque": analyse["type_risque"],
        "description_risque": analyse["description_risque"],
        "impact_risque": analyse.get("impact_risque", ""),
        "score_probabilite_1_5": analyse["score_probabilite_1_5"],
        "score_impact_1_5": analyse["score_impact_1_5"],
        "bloque_projet": analyse["bloquer_projet"],
        "strategie_de_reponse": analyse["strategie_de_reponse"],
        "owner_contact": expediteur
    }
    
    success = sauvegarder_risque_complet(db_data, message_id)
    
    if not success:
        envoyer_whatsapp(expediteur, "Erreur sauvegarde. Reessayez avec #.")
        return True
    
    # Récupérer pour avoir score_global et priorite calculés
    risque_complet = recuperer_risque_par_id(risque_id)
    score_global = risque_complet.get("score_global", 15) if risque_complet else 15
    priorite = risque_complet.get("priorite", "ELEVE") if risque_complet else "ELEVE"
    
    # Feedback technicien
    feedback = (
        f"RISQUE ENREGISTRE - {risque_id}\n\n"
        f"Projet: {data['type_projet']}\n"
        f"Nom: {data['nom_projet']}\n"
        f"Site: {data['site']}\n"
        f"Type: {analyse['type_risque']}\n"
        f"Priorite: {priorite}\n"
        f"Score: {score_global}/25\n"
        f"Probabilite: {analyse['score_probabilite_1_5']}/5\n"
        f"Impact: {analyse['score_impact_1_5']}/5\n"
        f"Strategie: {analyse['strategie_de_reponse']}\n"
        f"Bloque: {'OUI' if analyse['bloquer_projet'] else 'NON'}\n\n"
        f"Action: {analyse['action_immediate']}\n"
        f"Confiance IA: {analyse.get('confiance', 0):.0%}\n"
        f"Justification: {analyse.get('justification_scores', 'N/A')}\n\n"
        f"Votre responsable a ete alerte.\n"
        f"Dashboard: {LIEN_DASHBOARD}\n\n"
        f"Pour fermer ce risque, envoyez:\n"
        f"CLOSE {risque_id}"
    )
    envoyer_whatsapp(expediteur, feedback)
    
    # Nettoyer conversation
    if expediteur in conversations:
        del conversations[expediteur]
    
    # Alertes managers
    destinataires = ESCALADE.get(priorite, [])
    if destinataires:
        alerte = (
            f"ALERTE {priorite} - {data['type_projet']}\n"
            f"ID: {risque_id}\n"
            f"Nom projet: {data['nom_projet']}\n"
            f"Site: {data['site']}\n"
            f"Signale: {expediteur}\n"
            f"Desc: {analyse['description_risque']}\n"
            f"Impact: {analyse['impact_risque']}\n"
            f"Score: {score_global}/25 (P{analyse['score_probabilite_1_5']}xI{analyse['score_impact_1_5']})\n"
            f"Action: {analyse['action_immediate']}\n"
            f"Bloque: {'OUI' if analyse['bloquer_projet'] else 'NON'}\n"
            f"Confiance IA: {analyse.get('confiance', 0):.0%}\n\n"
            f"Dashboard: {LIEN_DASHBOARD}\n\n"
            f"Reagissez:\n"
            f"👍 = Pris en charge\n"
            f"⬆️ = Escalader\n\n"
            f"Pour fermer: CLOSE {risque_id}"
        )
        
        for role in destinataires:
            astreinte = get_astreinte(role)
            if astreinte and astreinte.get("telephone"):
                envoyer_whatsapp(astreinte["telephone"], alerte)
    
    return True

# ============================================================
# COMMANDES MANAGER
# ============================================================

def traiter_commande_manager(expediteur, message):
    msg_upper = message.strip().upper()
    
    # CLOSE XXXAAMM0000
    match_close = re.match(r'^(CLOSE|CLOSED)\s+([A-Z]{3}\d{8})$', msg_upper)
    if match_close:
        risque_id = match_close.group(2)
        risque = recuperer_risque_par_id(risque_id)
        
        if not risque:
            envoyer_whatsapp(expediteur, f"Risque {risque_id} introuvable.")
            return True
        
        if risque.get("statut") == "CLOSED":
            envoyer_whatsapp(expediteur, f"{risque_id} deja ferme.")
            return True
        
        fermer_risque(risque_id, "Fermeture via commande WhatsApp", expediteur)
        
        envoyer_whatsapp(expediteur,
            f"Risque {risque_id} ferme.\n"
            f"Nom: {risque.get('nom_projet')}\n"
            f"Site: {risque.get('site')}\n"
            f"Date: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        
        envoyer_whatsapp(risque.get("source_stakeholder_contact"),
            f"Votre signalement {risque_id} a ete ferme.\n"
            f"Merci pour votre vigilance.")
        return True
    
    # STATUS XXXAAMM0000
    match_status = re.match(r'^STATUS\s+([A-Z]{3}\d{8})$', msg_upper)
    if match_status:
        risque_id = match_status.group(1)
        risque = recuperer_risque_par_id(risque_id)
        
        if not risque:
            envoyer_whatsapp(expediteur, f"{risque_id} introuvable.")
            return True
        
        envoyer_whatsapp(expediteur,
            f"STATUT {risque_id}\n"
            f"Nom: {risque.get('nom_projet')}\n"
            f"Site: {risque.get('site')}\n"
            f"Priorite: {risque.get('priorite')}\n"
            f"Score: {risque.get('score_global')}/25\n"
            f"Statut: {risque.get('statut')}\n"
            f"Owner: {risque.get('owner_nom') or risque.get('owner_contact')}\n"
            f"Date: {str(risque.get('date_identification', ''))[:16]}")
        return True
    
    return False

# ============================================================
# RECHERCHE PAR PERIODE
# ============================================================

def traiter_recherche_periode(expediteur, message):
    msg_upper = message.strip().upper()
    
    # LISTE MAI [2026]
    match_liste = re.match(r'^LISTE\s+([A-ZÉÈÊ]+)(?:\s+(\d{4}))?$', msg_upper)
    if match_liste:
        mois_str = match_liste.group(1)
        annee_str = match_liste.group(2)
        
        mois_map = {
            'JANVIER': 1, 'JAN': 1, 'FEVRIER': 2, 'FEV': 2,
            'MARS': 3, 'MAR': 3, 'AVRIL': 4, 'AVR': 4,
            'MAI': 5, 'JUIN': 6, 'JUI': 6, 'JUILLET': 7,
            'AOUT': 8, 'AOU': 8, 'SEPTEMBRE': 9, 'SEP': 9,
            'OCTOBRE': 10, 'OCT': 10, 'NOVEMBRE': 11, 'NOV': 11,
            'DECEMBRE': 12, 'DEC': 12
        }
        
        if mois_str.isdigit():
            return envoyer_liste_annee(expediteur, int(mois_str))
        
        mois = mois_map.get(mois_str)
        if not mois:
            envoyer_whatsapp(expediteur, "Mois non reconnu.")
            return True
        
        annee = int(annee_str) if annee_str else datetime.now().year
        return envoyer_liste_mois(expediteur, annee, mois)
    
    # STATS MAI [2026]
    match_stats = re.match(r'^STATS\s+([A-ZÉÈÊ]+)(?:\s+(\d{4}))?$', msg_upper)
    if match_stats:
        mois_str = match_stats.group(1)
        annee_str = match_stats.group(2)
        
        mois_map = {
            'JANVIER': 1, 'JAN': 1, 'FEVRIER': 2, 'FEV': 2,
            'MARS': 3, 'MAR': 3, 'AVRIL': 4, 'AVR': 4,
            'MAI': 5, 'JUIN': 6, 'JUI': 6, 'JUILLET': 7,
            'AOUT': 8, 'AOU': 8, 'SEPTEMBRE': 9, 'SEP': 9,
            'OCTOBRE': 10, 'OCT': 10, 'NOVEMBRE': 11, 'NOV': 11,
            'DECEMBRE': 12, 'DEC': 12
        }
        
        if mois_str.isdigit():
            envoyer_whatsapp(expediteur, "Pour stats annuelles: STATS 2026")
            return True
        
        mois = mois_map.get(mois_str)
        if not mois:
            envoyer_whatsapp(expediteur, "Mois non reconnu.")
            return True
        
        annee = int(annee_str) if annee_str else datetime.now().year
        return envoyer_stats_mois(expediteur, annee, mois)
    
    return False

def envoyer_liste_mois(expediteur, annee, mois):
    if not supabase:
        envoyer_whatsapp(expediteur, "Base non disponible.")
        return True
    
    aa = str(annee)[-2:]
    mm = f"{mois:02d}"
    pattern = f"{aa}{mm}"
    
    try:
        result = supabase.table("risques").select("*").like("risque_id", f"___{pattern}%").execute()
        risques = result.data if result.data else []
        
        if not risques:
            envoyer_whatsapp(expediteur, f"Aucun risque pour {mois:02d}/{annee}.")
            return True
        
        msg = f"RISQUES {mois:02d}/{annee} ({len(risques)}):\n\n"
        for i, r in enumerate(risques[:10], 1):
            msg += (
                f"{i}. {r['risque_id']}\n"
                f"   {r['type_projet']} - {r['site']}\n"
                f"   {r['priorite']} | {r['score_global']}/25\n\n"
            )
        
        if len(risques) > 10:
            msg += f"... et {len(risques) - 10} autres.\n"
        
        msg += f"Dashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg)
        return True
        
    except Exception as e:
        print(f"==> Erreur liste mois: {e}")
        envoyer_whatsapp(expediteur, "Erreur recuperation.")
        return True

def envoyer_liste_annee(expediteur, annee):
    if not supabase:
        envoyer_whatsapp(expediteur, "Base non disponible.")
        return True
    
    try:
        aa = str(annee)[-2:]
        result = supabase.table("risques").select("*").like("risque_id", f"___{aa}%").execute()
        risques = result.data if result.data else []
        
        if not risques:
            envoyer_whatsapp(expediteur, f"Aucun risque pour {annee}.")
            return True
        
        par_mois = {}
        for r in risques:
            mm = r['risque_id'][6:8]
            if mm not in par_mois:
                par_mois[mm] = []
            par_mois[mm].append(r)
        
        msg = f"RISQUES {annee} ({len(risques)}):\n\n"
        for mm in sorted(par_mois.keys()):
            mois_risques = par_mois[mm]
            critiques = sum(1 for r in mois_risques if r.get('priorite') == 'CRITIQUE')
            msg += f"Mois {mm}: {len(mois_risques)} ({critiques} critiques)\n"
        
        msg += f"\nDashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg)
        return True
        
    except Exception as e:
        print(f"==> Erreur liste annee: {e}")
        envoyer_whatsapp(expediteur, "Erreur recuperation.")
        return True

def envoyer_stats_mois(expediteur, annee, mois):
    if not supabase:
        envoyer_whatsapp(expediteur, "Base non disponible.")
        return True
    
    try:
        result = supabase.rpc("stats_periode", {"annee": annee, "mois": mois}).execute()
        stats = result.data[0] if result.data else None
        
        if not stats:
            envoyer_whatsapp(expediteur, f"Stats non dispos pour {mois:02d}/{annee}.")
            return True
        
        msg = (
            f"STATS {mois:02d}/{annee}\n\n"
            f"Total: {stats.get('total', 0)}\n"
            f"Bloquants: {stats.get('bloquants', 0)}\n"
            f"Fermes: {stats.get('fermes', 0)}\n"
            f"Score moyen: {stats.get('moyenne_score', 0):.2f}/25\n\n"
            f"Par priorite:\n"
        )
        
        par_prio = stats.get('par_priorite', {})
        for prio in ['CRITIQUE', 'ELEVE', 'MOYEN', 'FAIBLE']:
            if prio in par_prio:
                msg += f"  {prio}: {par_prio[prio]}\n"
        
        msg += f"\nDashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg)
        return True
        
    except Exception as e:
        print(f"==> Erreur stats mois: {e}")
        envoyer_whatsapp(expediteur, "Erreur stats.")
        return True

# ============================================================
# RAPPORT HEBDO
# ============================================================

def generer_rapport_hebdo():
    if not supabase:
        print("==> Supabase non configure")
        return
    
    try:
        debut = (datetime.now() - timedelta(days=7)).isoformat()
        result = supabase.table("risques").select("*").gte("date_identification", debut).execute()
        risques = result.data
        
        total = len(risques)
        par_priorite = {}
        par_type_projet = {}
        par_type_risque = {}
        moy_score = 0
        bloquants = 0
        
        for r in risques:
            p = r.get("priorite", "MOYEN")
            par_priorite[p] = par_priorite.get(p, 0) + 1
            tp = r.get("type_projet", "AUTRES")
            par_type_projet[tp] = par_type_projet.get(tp, 0) + 1
            tr = r.get("type_risque", "AUTRES")
            par_type_risque[tr] = par_type_risque.get(tr, 0) + 1
            moy_score += r.get("score_global", 0) or 0
            if r.get("bloque_projet"):
                bloquants += 1
        
        moy_score = moy_score / total if total else 0
        
        prompt = f"""Rapport semaine {datetime.now().isocalendar()[1]}

STATS:
- Total: {total}
- Par priorite: {json.dumps(par_priorite, ensure_ascii=False)}
- Par projet: {json.dumps(par_type_projet, ensure_ascii=False)}
- Score moyen: {moy_score:.1f}/25
- Bloquants: {bloquants}

Genere rapport WhatsApp concis:
1. Resume (2 lignes)
2. Top 3 risques
3. Tendances
4. Score sante (0-100)
5. Decisions urgentes"""

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800
        )
        rapport = response.choices[0].message.content
        
        supabase.table("rapports_hebdo").insert({
            "semaine": datetime.now().isocalendar()[1],
            "annee": datetime.now().year,
            "contenu": rapport,
            "kpi_json": {
                "total": total,
                "par_priorite": par_priorite,
                "par_type_projet": par_type_projet,
                "score_moyen": moy_score,
                "bloquants": bloquants
            },
            "date_envoi": datetime.now().isoformat()
        }).execute()
        
        for role in ["CHEF_PROJET", "PMO", "DIRECTEUR"]:
            astreinte = get_astreinte(role)
            if astreinte and astreinte.get("telephone"):
                envoyer_whatsapp(astreinte["telephone"],
                    f"RAPPORT HEBDO DigitalRisk\n\n{rapport}\n\nDashboard: {LIEN_DASHBOARD}")
                    
    except Exception as e:
        print(f"==> Erreur rapport: {e}")

# ============================================================
# ESCALADE TEMPORELLE
# ============================================================

def verifier_escalades_en_attente():
    if not supabase:
        return
    
    try:
        result = supabase.table("risques").select("*").eq("statut", "OPENED").lte(
            "date_identification",
            (datetime.now() - timedelta(minutes=30)).isoformat()
        ).execute()
        
        for alerte in result.data:
            priorite = alerte.get("priorite", "MOYEN")
            cree = datetime.fromisoformat(alerte["date_identification"].replace("Z", "+00:00"))
            minutes = (datetime.now() - cree).total_seconds() / 60
            
            regles = {
                "CRITIQUE": (15, "CHEF_PROJET", "PMO"),
                "ELEVE": (30, "CHEF_PROJET", "DIRECTEUR"),
                "MOYEN": (60, "CHEF_PROJET", "DIRECTEUR")
            }
            
            if priorite in regles:
                delai, _, niveau2 = regles[priorite]
                if minutes > delai and alerte.get("niveau_escalade", 0) < 1:
                    astreinte = get_astreinte(niveau2)
                    if astreinte and astreinte.get("telephone"):
                        envoyer_whatsapp(astreinte["telephone"],
                            f"ESCALADE [{priorite}] - {delai}min sans action\n"
                            f"ID: {alerte['risque_id']}\n"
                            f"Projet: {alerte['nom_projet']}\n"
                            f"Site: {alerte['site']}\n"
                            f"Desc: {alerte['description_risque']}\n"
                            f"Signale: {alerte['source_stakeholder_contact']}\n\n"
                            f"Dashboard: {LIEN_DASHBOARD}")
                        mettre_a_jour_risque(alerte["risque_id"], {"niveau_escalade": 1})
    except Exception as e:
        print(f"==> Erreur escalade: {e}")

# ============================================================
# ROUTES FLASK
# ============================================================

@app.route("/")
def home():
    return "DigitalRiskPlatform v4.0 - XXXAAMM0000 + Recherche Periode", 200

@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "digital_risk_2026":
        return request.args.get("hub.challenge")
    return "Erreur verification", 403

@app.route("/webhook", methods=["POST"])
def recevoir_message():
    if not verifier_signature(request):
        return jsonify({"error": "Signature invalide"}), 403
    
    data = request.json
    print(json.dumps(data, indent=2, ensure_ascii=False))
    
    try:
        entry = data.get("entry", [])
        if not entry:
            return jsonify({"status": "ok"})
        
        changes = entry[0].get("changes", [])
        if not changes:
            return jsonify({"status": "ok"})
        
        value = changes[0].get("value", {})
        
        # Reactions emoji
        if "messages" in value:
            for msg in value["messages"]:
                if msg.get("type") == "reaction":
                    emoji = msg["reaction"].get("emoji")
                    msg_id = msg["reaction"].get("message_id")
                    expediteur = msg["from"]
                    
                    if emoji in ["👍", "✅", "🆗"]:
                        if supabase:
                            result = supabase.table("risques").select("risque_id").eq("message_id_whatsapp", msg_id).execute()
                            if result.data:
                                rid = result.data[0]["risque_id"]
                                mettre_a_jour_risque(rid, {
                                    "statut": "ASSIGNED",
                                    "owner_contact": expediteur
                                })
                                envoyer_whatsapp(expediteur, f"Risque {rid} - Pris en charge.")
                        return jsonify({"status": "pris_en_charge"})
                    
                    elif emoji in ["⬆️", "🔴", "⚠️"]:
                        envoyer_whatsapp(expediteur, "Escalade notee.")
                        return jsonify({"status": "escalade"})
        
        # Messages textes
        if "messages" not in value:
            return jsonify({"status": "ok"})
        
        msg_data = value["messages"][0]
        msg_id = msg_data["id"]
        msg_type = msg_data.get("type", "text")
        
        if msg_type != "text":
            return jsonify({"status": "ok"})
        
        message = msg_data["text"]["body"]
        expediteur = msg_data["from"]
        
        print(f"==> Message: {message}")
        print(f"==> Expediteur: {expediteur}")
        
        # Commandes manager (CLOSE, STATUS)
        if traiter_commande_manager(expediteur, message):
            return jsonify({"status": "commande_manager"})
        
        # Recherche periode (LISTE, STATS)
        if traiter_recherche_periode(expediteur, message):
            return jsonify({"status": "recherche_periode"})
        
        # Commandes generales
        cmd = message.strip().upper()
        if cmd in ["RAPPORT", "DASHBOARD", "STATS"]:
            envoyer_whatsapp(expediteur, f"Dashboard: {LIEN_DASHBOARD}")
            return jsonify({"status": "ok"})
        
        if cmd in ["AIDE", "HELP", "MENU"]:
            envoyer_whatsapp(expediteur,
                "DigitalRiskPlatform\n\n"
                "SIGNALER UN RISQUE:\n"
                "Envoyez # pour commencer\n\n"
                "RECHERCHE:\n"
                "LISTE MAI [2026] - Risques du mois\n"
                "LISTE 2026 - Risques de l'annee\n"
                "STATS MAI [2026] - Statistiques\n\n"
                "MANAGERS:\n"
                "CLOSE XXXAAMM0000 - Fermer\n"
                "STATUS XXXAAMM0000 - Voir statut\n\n"
                "GENERAL:\n"
                "RAPPORT - Dashboard\n"
                "AIDE - Ce menu")
            return jsonify({"status": "ok"})
        
        # Formulaire interactif
        if traiter_etape_conversation(expediteur, message, msg_id):
            return jsonify({"status": "conversation"})
        
        return jsonify({"status": "ok"})
        
    except Exception as e:
        import traceback
        print(f"ERREUR: {e}")
        print(traceback.format_exc())
    
    return jsonify({"status": "ok"})

# ============================================================
# DEMARRAGE
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
