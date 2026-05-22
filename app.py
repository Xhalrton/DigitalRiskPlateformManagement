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
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

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

CONTACTS = {
    "CHEF_PROJET": "+2250500277071",
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

STATUTS_VALIDES = ["OPENED", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED"]

_groq_client = None
_supabase_client = None

def get_groq_client():
    global _groq_client
    if _groq_client is None and GROQ_KEY:
        _groq_client = Groq(api_key=GROQ_KEY)
    return _groq_client

def get_supabase():
    global _supabase_client
    if _supabase_client is None and SUPABASE_URL and SUPABASE_KEY:
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client

LIEN_DASHBOARD = os.environ.get("DASHBOARD_URL", "https://google.com")

# ============================================================
# SESSIONS PERSISTANTES
# ============================================================

def get_conversation(numero):
    supabase = get_supabase()
    if not supabase:
        return None
    try:
        result = supabase.table("sessions_wa").select("*").eq("numero", numero).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        print(f"==> Erreur get_conversation: {e}")
        return None

def set_conversation(numero, etape, data):
    supabase = get_supabase()
    if not supabase:
        return
    try:
        supabase.table("sessions_wa").upsert({
            "numero": numero,
            "etape": etape,
            "data": data,
            "updated_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        print(f"==> Erreur set_conversation: {e}")

def delete_conversation(numero):
    supabase = get_supabase()
    if not supabase:
        return
    try:
        supabase.table("sessions_wa").delete().eq("numero", numero).execute()
    except Exception as e:
        print(f"==> Erreur delete_conversation: {e}")

# ============================================================
# IDEMPOTENCE
# ============================================================

def message_deja_traite(message_id):
    supabase = get_supabase()
    if not supabase:
        return False
    try:
        result = supabase.table("messages_traites").select("message_id").eq("message_id", message_id).execute()
        return bool(result.data)
    except Exception as e:
        print(f"==> Erreur idempotence check: {e}")
        return False

def marquer_message_traite(message_id):
    supabase = get_supabase()
    if not supabase:
        return
    try:
        supabase.table("messages_traites").insert({"message_id": message_id}).execute()
    except Exception as e:
        print(f"==> Erreur idempotence insert: {e}")

# ============================================================
# SECURITE
# ============================================================

def verifier_signature(req):
    if not WA_APP_SECRET or WA_APP_SECRET == "placeholder":
        return True
    signature = req.headers.get('X-Hub-Signature-256', '')
    if not signature:
        return False
    expected = hmac.new(
        WA_APP_SECRET.encode('utf-8'),
        req.get_data(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

# ============================================================
# GENERATION ID
# ============================================================

def get_prefix(type_projet):
    supabase = get_supabase()
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
    prefix = get_prefix(type_projet)
    now = datetime.now()
    aa = str(now.year)[-2:]
    mm = f"{now.month:02d}"
    base_prefix = f"{prefix}{aa}{mm}"
    supabase = get_supabase()
    if supabase:
        try:
            result = supabase.table("risques").select("risque_id").like("risque_id", f"{base_prefix}%").execute()
            count = len(result.data) if result.data else 0
        except Exception as e:
            print(f"==> Erreur comptage: {e}")
            count = 0
    else:
        count = 0
    return f"{base_prefix}{count + 1:04d}"

# ============================================================
# SUPABASE
# ============================================================

def sauvegarder_risque_complet(data, message_id=None):
    supabase = get_supabase()
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
    supabase = get_supabase()
    if not supabase:
        return None
    try:
        result = supabase.table("risques").select("*").eq("risque_id", risque_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        print(f"==> Erreur recup: {e}")
        return None

def mettre_a_jour_risque(risque_id, updates):
    supabase = get_supabase()
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
        "owner_contact": par_qui,
        "date_resolution": datetime.now().isoformat()
    })

def get_astreinte(role):
    supabase = get_supabase()
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
    if WA_TOKEN == "placeholder" or not WA_TOKEN:
        print(f"==> [SIMULATION] WhatsApp a {numero}: {message[:50]}...")
        return None
    if WA_PHONE_ID == "placeholder" or not WA_PHONE_ID:
        print(f"==> [ERREUR] WA_PHONE_ID non configure")
        return None

    url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "text",
        "text": {"body": message}
    }
    if message_id_reference:
        data["context"] = {"message_id": message_id_reference}

    try:
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        print(f"==> WhatsApp a {numero}: {resp.status_code}")
        if resp.status_code != 200:
            print(f"==> ERREUR: {resp.text}")
        return resp.json().get("messages", [{}])[0].get("id")
    except Exception as e:
        print(f"==> Erreur envoi: {e}")
        return None

# ============================================================
# AMELIORATION 3 : IA ENRICHIE — analyse en français, plus de détails
# ============================================================

def analyser_risque_ia(description, site, type_projet, nom_signalant=""):
    groq_client = get_groq_client()
    if not groq_client:
        return {
            "type_risque": "AUTRES",
            "description_risque": description[:100],
            "impact_risque": "À évaluer",
            "mesures_preventives": "Évaluation sur site requise",
            "score_probabilite_1_5": 3,
            "score_impact_1_5": 3,
            "bloquer_projet": False,
            "strategie_de_reponse": "REDUCTION",
            "action_immediate": "Évaluation sur site requise",
            "action_court_terme": "À définir sous 24h",
            "action_long_terme": "À planifier",
            "parties_prenantes": "Équipe projet",
            "confiance": 0.3,
            "justification_scores": "IA non disponible - défaut"
        }

    prompt = f"""Tu es un expert senior en gestion des risques pour les projets télécoms en Afrique de l'Ouest.
Tu travailles pour une entreprise de déploiement réseau en Côte d'Ivoire.
Tu rédiges toujours en FRANÇAIS PARFAIT, professionnel et précis.

CONTEXTE DU SIGNALEMENT:
- Type de projet: {type_projet}
- Site concerné: {site}
- Signalé par: {nom_signalant or 'Non renseigné'}
- Description: "{description}"
- Date et heure: {datetime.now().strftime('%d/%m/%Y à %H:%M')}

Analyse ce risque de manière exhaustive et réponds UNIQUEMENT en JSON valide:

{{
  "type_risque": "ACCES|SECURITE|TECHNIQUE|ADMINISTRATIF|METEO|LOGISTIQUE|SANITAIRE|SOCIAL|AUTRES",
  "description_risque": "Synthèse claire et professionnelle du risque en 2-3 phrases",
  "impact_risque": "Description précise de l'impact opérationnel sur le projet et les équipes",
  "mesures_preventives": "Mesures préventives à mettre en place pour éviter la récurrence",
  "score_probabilite_1_5": 1,
  "score_impact_1_5": 1,
  "bloquer_projet": false,
  "strategie_de_reponse": "EVITEMENT|REDUCTION|TRANSFERT|ACCEPTATION",
  "action_immediate": "Action concrète et urgente à réaliser dans l'heure",
  "action_court_terme": "Action à réaliser dans les 24 à 48 heures",
  "action_long_terme": "Action stratégique à planifier sur le moyen terme",
  "parties_prenantes": "Liste des personnes ou entités à impliquer",
  "confiance": 0.85,
  "justification_scores": "Justification en 20 mots maximum des scores attribués"
}}

RÈGLES DE SCORING:
- Probabilité: 1=quasi impossible, 2=peu probable, 3=possible, 4=probable, 5=quasi certain
- Impact: 1=négligeable, 2=mineur, 3=modéré, 4=majeur, 5=catastrophique
- Score global = probabilité × impact (max 25)
- bloquer_projet = true si score >= 15 ou si danger physique immédiat

CONSIGNES QUALITÉ:
- Rédige en français professionnel sans fautes
- Sois précis et actionnable dans les recommandations
- Adapte les actions au contexte télécom en Côte d'Ivoire
- Ne renvoie que le JSON, rien d'autre"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1000,
            timeout=20
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
            "impact_risque": "À évaluer",
            "mesures_preventives": "Évaluation sur site requise",
            "score_probabilite_1_5": 3,
            "score_impact_1_5": 3,
            "bloquer_projet": False,
            "strategie_de_reponse": "REDUCTION",
            "action_immediate": "Évaluation sur site requise",
            "action_court_terme": "À définir sous 24h",
            "action_long_terme": "À planifier",
            "parties_prenantes": "Équipe projet",
            "confiance": 0.3,
            "justification_scores": "Erreur IA - défaut"
        }

# ============================================================
# AMELIORATION 1 : FORMULAIRE 5 ETAPES + messages en majuscules
# Etape 1 : NOM & PRENOM du signalant (NOUVEAU)
# Etape 2 : Type de projet
# Etape 3 : NOM DU PROJET
# Etape 4 : SITE
# Etape 5 : DESCRIPTION
# Etape 6 : Confirmation
# ============================================================

def envoyer_formulaire_etape(numero, etape, data=None, message_id=None):
    messages = {
        1: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 1/5 — IDENTITE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Indiquez votre NOM et PRENOM.\n\n"
            "Exemple: KOUASSI Jean-Baptiste"
        ),
        2: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 2/5 — TYPE DE PROJET\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1. RAN\n"
            "2. RURAL\n"
            "3. FIBRE\n"
            "4. CORE\n"
            "5. IPRAN\n"
            "6. MWV\n"
            "7. MMONEY\n"
            "8. HOME\n"
            "9. AUTRES\n\n"
            "Repondez par le NUMERO ou le NOM."
        ),
        3: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 3/5 — NOM DU PROJET\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Indiquez le NOM PRECIS du projet.\n\n"
            "Exemple: DEPLOIEMENT RAN ABIDJAN NORD"
        ),
        4: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 4/5 — SITE CONCERNE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Indiquez le NOM ou CODE du site.\n\n"
            "Exemple: COCODY, ABJ_COC_001"
        ),
        5: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 5/5 — DESCRIPTION DU RISQUE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Decrivez le risque en detail:\n\n"
            "• QUEL EST LE PROBLEME ?\n"
            "• QUAND A-T-IL COMMENCE ?\n"
            "• PERSONNES EN DANGER ?\n"
            "• TRAVAUX BLOQUES ?\n\n"
            "Soyez precis — l'IA analysera votre message."
        ),
    }

    if etape == 6 and data:
        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ RESUME DU SIGNALEMENT\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 SIGNALE PAR : {data.get('nom_signalant', '?').upper()}\n"
            f"📁 PROJET : {data.get('type_projet', '?')}\n"
            f"🏗️ NOM : {data.get('nom_projet', '?').upper()}\n"
            f"📍 SITE : {data.get('site', '?').upper()}\n"
            f"⚠️ DESCRIPTION : {data.get('description', '?')[:120]}...\n\n"
            "Envoyez *OK* pour valider et analyser\n"
            "ou *ANNULER* pour recommencer."
        )
    else:
        msg = messages.get(etape, "Etape inconnue.")

    envoyer_whatsapp(numero, msg, message_id)

def traiter_etape_conversation(expediteur, message, message_id):
    msg_upper = message.strip().upper()
    conv = get_conversation(expediteur)

    # Nouveau signalement
    if not conv:
        if not msg_upper.startswith("#"):
            envoyer_whatsapp(expediteur,
                "Pour signaler un risque, votre message doit commencer par #\n\n"
                "Envoyez *#* pour commencer.", message_id)
            return True
        set_conversation(expediteur, 1, {})
        envoyer_formulaire_etape(expediteur, 1, message_id=message_id)
        return True

    etape = conv["etape"]
    data = conv["data"] if isinstance(conv["data"], dict) else {}

    # ANNULATION possible à tout moment
    if msg_upper in ["ANNULER", "CANCEL", "RESET"]:
        delete_conversation(expediteur)
        envoyer_whatsapp(expediteur,
            "❌ Signalement annulé.\n\nEnvoyez *#* pour recommencer.", message_id)
        return True

    # Etape 1 : NOM & PRENOM (NOUVEAU)
    if etape == 1:
        nom = message.strip()
        if len(nom) < 3:
            envoyer_whatsapp(expediteur,
                "⚠️ Veuillez indiquer votre NOM et PRENOM complets.\n\nExemple: KOUASSI Jean-Baptiste", message_id)
            return True
        data["nom_signalant"] = nom
        set_conversation(expediteur, 2, data)
        envoyer_formulaire_etape(expediteur, 2, message_id=message_id)
        return True

    # Etape 2 : Type projet
    elif etape == 2:
        type_proj = None
        for i, tp in enumerate(TYPES_PROJET, 1):
            if str(i) == msg_upper or tp == msg_upper:
                type_proj = tp
                break
        if not type_proj:
            envoyer_whatsapp(expediteur,
                "⚠️ TYPE NON RECONNU.\n\nChoisissez parmi:\nRAN, RURAL, FIBRE, CORE, IPRAN, MWV, MMONEY, HOME, AUTRES\n\nou repondez par le numero (1 a 9).", message_id)
            return True
        data["type_projet"] = type_proj
        set_conversation(expediteur, 3, data)
        envoyer_formulaire_etape(expediteur, 3, message_id=message_id)
        return True

    # Etape 3 : Nom projet
    elif etape == 3:
        data["nom_projet"] = message.strip()
        set_conversation(expediteur, 4, data)
        envoyer_formulaire_etape(expediteur, 4, message_id=message_id)
        return True

    # Etape 4 : Site
    elif etape == 4:
        data["site"] = message.strip()
        set_conversation(expediteur, 5, data)
        envoyer_formulaire_etape(expediteur, 5, message_id=message_id)
        return True

    # Etape 5 : Description
    elif etape == 5:
        if len(message.strip()) < 10:
            envoyer_whatsapp(expediteur,
                "⚠️ DESCRIPTION TROP COURTE.\n\nMerci de décrire le risque plus en détail.", message_id)
            return True
        data["description"] = message.strip()
        set_conversation(expediteur, 6, data)
        envoyer_formulaire_etape(expediteur, 6, data, message_id=message_id)
        return True

    # Etape 6 : Confirmation
    elif etape == 6:
        if msg_upper in ["OK", "OUI", "VALIDER"]:
            return traiter_risque_confirme(expediteur, data, message_id)
        elif msg_upper in ["ANNULER", "NON", "RESET"]:
            delete_conversation(expediteur)
            envoyer_whatsapp(expediteur, "❌ Annulé. Envoyez *#* pour recommencer.", message_id)
            return True
        else:
            envoyer_whatsapp(expediteur, "Repondez *OK* pour valider ou *ANNULER* pour recommencer.", message_id)
            return True

    return False

def traiter_risque_confirme(expediteur, data, message_id):
    envoyer_whatsapp(expediteur,
        "⏳ ANALYSE IA EN COURS...\n\nVeuillez patienter quelques secondes.", message_id)

    nom_signalant = data.get("nom_signalant", "")
    analyse = analyser_risque_ia(data["description"], data["site"], data["type_projet"], nom_signalant)

    risque_id = generer_risque_id(data["type_projet"])

    db_data = {
        "risque_id": risque_id,
        "source_stakeholder_contact": expediteur,
        "source_stakeholder_nom_prenom": nom_signalant,
        "message_id_signalant": message_id,
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
        envoyer_whatsapp(expediteur,
            "❌ ERREUR DE SAUVEGARDE.\n\nVeuillez reessayer avec *#*.", message_id)
        return True

    risque_complet = recuperer_risque_par_id(risque_id)
    score_global = risque_complet.get("score_global", 15) if risque_complet else 15
    priorite = risque_complet.get("priorite", "ELEVE") if risque_complet else "ELEVE"

    # Emoji selon priorité
    emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}.get(priorite, "⚪")

    # AMELIORATION 1 : feedback enrichi avec champs en majuscules
    feedback = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ RISQUE ENREGISTRE\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 ID : *{risque_id}*\n"
        f"👤 SIGNALE PAR : {nom_signalant.upper()}\n"
        f"📁 PROJET : {data['type_projet']}\n"
        f"🏗️ NOM PROJET : {data['nom_projet'].upper()}\n"
        f"📍 SITE : {data['site'].upper()}\n"
        f"🔖 TYPE : {analyse['type_risque']}\n"
        f"{emoji_priorite} PRIORITE : {priorite}\n"
        f"📊 SCORE : {score_global}/25 "
        f"(P{analyse['score_probabilite_1_5']} × I{analyse['score_impact_1_5']})\n"
        f"🚧 BLOQUE : {'OUI ⚠️' if analyse['bloquer_projet'] else 'NON'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 ANALYSE IA\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 DESCRIPTION : {analyse['description_risque']}\n\n"
        f"💥 IMPACT : {analyse['impact_risque']}\n\n"
        f"⚡ ACTION IMMEDIATE : {analyse['action_immediate']}\n\n"
        f"📅 COURT TERME : {analyse.get('action_court_terme', 'N/A')}\n\n"
        f"🛡️ PREVENTION : {analyse.get('mesures_preventives', 'N/A')}\n\n"
        f"🎯 STRATEGIE : {analyse['strategie_de_reponse']}\n"
        f"🔍 CONFIANCE IA : {analyse.get('confiance', 0):.0%}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Votre responsable a ete alerte.\n"
        f"🌐 Dashboard: {LIEN_DASHBOARD}\n\n"
        f"Pour fermer ce risque:\n"
        f"*CLOSE {risque_id}*"
    )
    envoyer_whatsapp(expediteur, feedback, message_id)
    delete_conversation(expediteur)

    destinataires = ESCALADE.get(priorite, [])
    if destinataires:
        alerte = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚨 ALERTE {priorite} {emoji_priorite}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 ID : *{risque_id}*\n"
            f"📁 PROJET : {data['type_projet']}\n"
            f"🏗️ NOM PROJET : {data['nom_projet'].upper()}\n"
            f"📍 SITE : {data['site'].upper()}\n"
            f"👤 SIGNALE PAR : {nom_signalant.upper()}\n"
            f"📞 CONTACT : {expediteur}\n\n"
            f"📋 DESCRIPTION : {analyse['description_risque']}\n\n"
            f"💥 IMPACT : {analyse['impact_risque']}\n\n"
            f"📊 SCORE : {score_global}/25 "
            f"(P{analyse['score_probabilite_1_5']} × I{analyse['score_impact_1_5']})\n"
            f"🚧 BLOQUE : {'OUI ⚠️' if analyse['bloquer_projet'] else 'NON'}\n\n"
            f"⚡ ACTION IMMEDIATE : {analyse['action_immediate']}\n\n"
            f"👥 PARTIES PRENANTES : {analyse.get('parties_prenantes', 'N/A')}\n\n"
            f"🔍 CONFIANCE IA : {analyse.get('confiance', 0):.0%}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 Dashboard: {LIEN_DASHBOARD}\n\n"
            f"Reagissez:\n"
            f"👍 = Pris en charge\n"
            f"⬆️ = Escalader\n\n"
            f"Pour fermer: *CLOSE {risque_id}*"
        )
        for role in destinataires:
            astreinte = get_astreinte(role)
            if astreinte and astreinte.get("telephone"):
                envoyer_whatsapp(astreinte["telephone"], alerte, message_id)

    return True

# ============================================================
# COMMANDES MANAGER (CLOSE, STATUS, UPDATE, ASSIGN, MES-RISQUES)
# ============================================================

def traiter_commande_manager(expediteur, message, message_id=None):
    msg_upper = message.strip().upper()
    msg_original = message.strip()

    # --- CLOSE ---
    match_close = re.match(r'^(CLOSE|CLOSED)\s+([A-Z]{2,5}\d{8})$', msg_upper)
    if match_close:
        risque_id = match_close.group(2)
        risque = recuperer_risque_par_id(risque_id)
        if not risque:
            envoyer_whatsapp(expediteur, f"❌ Risque *{risque_id}* introuvable.", message_id)
            return True
        if risque.get("statut") == "CLOSED":
            envoyer_whatsapp(expediteur, f"ℹ️ *{risque_id}* est déjà fermé.", message_id)
            return True
        # Demander la raison de fermeture
        set_conversation(expediteur, "close_reason", {"risque_id": risque_id, "action": "CLOSE"})
        envoyer_whatsapp(expediteur,
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔒 FERMETURE *{risque_id}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📍 SITE : {risque.get('site', '?').upper()}\n"
            f"🏗️ PROJET : {risque.get('nom_projet', '?').upper()}\n\n"
            f"Décrivez l'ACTION MENEE pour résoudre ce risque:", message_id)
        return True

    # --- UPDATE STATUT ---
    # Format: UPDATE XXXAAMM0000 STATUT ACTION_MENEE
    match_update = re.match(r'^UPDATE\s+([A-Z]{2,5}\d{8})\s+(OPENED|ASSIGNED|IN_PROGRESS|RESOLVED|CLOSED)\s+(.+)$', msg_upper)
    if match_update:
        risque_id = match_update.group(1)
        nouveau_statut = match_update.group(2)
        # Récupérer l'action depuis le message original (pour garder la casse)
        parts = msg_original.split(None, 3)
        action_menee = parts[3] if len(parts) >= 4 else "Mise à jour du statut"

        risque = recuperer_risque_par_id(risque_id)
        if not risque:
            envoyer_whatsapp(expediteur, f"❌ Risque *{risque_id}* introuvable.", message_id)
            return True

        updates = {
            "statut": nouveau_statut,
            "reponse_apportee": action_menee,
            "owner_contact": expediteur
        }
        if nouveau_statut in ["RESOLVED", "CLOSED"]:
            updates["date_resolution"] = datetime.now().isoformat()

        mettre_a_jour_risque(risque_id, updates)

        emoji_statut = {
            "OPENED": "🔴", "ASSIGNED": "🟠", "IN_PROGRESS": "🔵",
            "RESOLVED": "🟢", "CLOSED": "⚫"
        }.get(nouveau_statut, "⚪")

        envoyer_whatsapp(expediteur,
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ STATUT MIS A JOUR\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 ID : *{risque_id}*\n"
            f"📍 SITE : {risque.get('site', '?').upper()}\n"
            f"{emoji_statut} NOUVEAU STATUT : {nouveau_statut}\n"
            f"📝 ACTION MENEE : {action_menee}\n"
            f"🕐 DATE : {datetime.now().strftime('%d/%m/%Y à %H:%M')}", message_id)

        # Notifier le signalant
        signalant = risque.get("source_stakeholder_contact")
        msg_id_signalant = risque.get("message_id_signalant")
        if signalant and signalant != expediteur:
            envoyer_whatsapp(signalant,
                f"ℹ️ Mise à jour de votre signalement *{risque_id}*\n\n"
                f"{emoji_statut} STATUT : {nouveau_statut}\n"
                f"📝 ACTION : {action_menee}\n"
                f"🕐 {datetime.now().strftime('%d/%m/%Y à %H:%M')}",
                msg_id_signalant)
        return True

    # --- ASSIGN ---
    # Format: ASSIGN XXXAAMM0000 +2250XXXXXXXX
    match_assign = re.match(r'^ASSIGN\s+([A-Z]{2,5}\d{8})\s+(\+?\d{10,15})$', msg_upper)
    if match_assign:
        risque_id = match_assign.group(1)
        numero_assignee = match_assign.group(2)
        if not numero_assignee.startswith("+"):
            numero_assignee = "+" + numero_assignee

        risque = recuperer_risque_par_id(risque_id)
        if not risque:
            envoyer_whatsapp(expediteur, f"❌ Risque *{risque_id}* introuvable.", message_id)
            return True

        mettre_a_jour_risque(risque_id, {
            "statut": "ASSIGNED",
            "owner_contact": numero_assignee
        })

        envoyer_whatsapp(expediteur,
            f"✅ *{risque_id}* assigné à *{numero_assignee}*\n"
            f"📍 SITE : {risque.get('site', '?').upper()}", message_id)

        # Notifier la personne assignée
        envoyer_whatsapp(numero_assignee,
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 RISQUE ASSIGNE\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 ID : *{risque_id}*\n"
            f"📁 PROJET : {risque.get('type_projet', '?')}\n"
            f"📍 SITE : {risque.get('site', '?').upper()}\n"
            f"⚠️ PRIORITE : {risque.get('priorite', '?')}\n\n"
            f"Vous etes responsable de ce risque.\n"
            f"Pour mettre a jour: *UPDATE {risque_id} IN_PROGRESS votre action*\n"
            f"Pour fermer: *CLOSE {risque_id}*")
        return True

    # --- STATUS ---
    match_status = re.match(r'^STATUS\s+([A-Z]{2,5}\d{8})$', msg_upper)
    if match_status:
        risque_id = match_status.group(1)
        risque = recuperer_risque_par_id(risque_id)
        if not risque:
            envoyer_whatsapp(expediteur, f"❌ *{risque_id}* introuvable.", message_id)
            return True

        emoji_statut = {
            "OPENED": "🔴", "ASSIGNED": "🟠", "IN_PROGRESS": "🔵",
            "RESOLVED": "🟢", "CLOSED": "⚫"
        }.get(risque.get("statut"), "⚪")
        emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}.get(
            risque.get("priorite"), "⚪")

        envoyer_whatsapp(expediteur,
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 STATUT *{risque_id}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📁 PROJET : {risque.get('type_projet', '?')}\n"
            f"🏗️ NOM : {str(risque.get('nom_projet', '?')).upper()}\n"
            f"📍 SITE : {str(risque.get('site', '?')).upper()}\n"
            f"👤 SIGNALE PAR : {str(risque.get('source_stakeholder_nom_prenom', risque.get('source_stakeholder_contact', '?'))).upper()}\n"
            f"{emoji_priorite} PRIORITE : {risque.get('priorite', '?')}\n"
            f"📊 SCORE : {risque.get('score_global', '?')}/25\n"
            f"{emoji_statut} STATUT : {risque.get('statut', '?')}\n"
            f"👤 OWNER : {risque.get('owner_contact', '?')}\n"
            f"📅 CREE : {str(risque.get('date_identification', '?'))[:16]}\n"
            f"📝 ACTION : {risque.get('reponse_apportee', 'Aucune action renseignee')}", message_id)
        return True

    # --- MES-RISQUES (liste par statut) ---
    match_mes = re.match(r'^MES-RISQUES(?:\s+(OPENED|ASSIGNED|IN_PROGRESS|RESOLVED|CLOSED))?$', msg_upper)
    if match_mes:
        statut_filtre = match_mes.group(1)
        return envoyer_liste_risques(expediteur, statut_filtre, message_id)

    # --- RISQUES-SITE ---
    match_site = re.match(r'^RISQUES-SITE\s+(.+)$', msg_upper)
    if match_site:
        site_recherche = match_site.group(1)
        return envoyer_risques_par_site(expediteur, site_recherche, message_id)

    return False

def envoyer_liste_risques(expediteur, statut_filtre=None, message_id=None):
    supabase = get_supabase()
    if not supabase:
        envoyer_whatsapp(expediteur, "❌ Base non disponible.", message_id)
        return True
    try:
        query = supabase.table("risques").select("*")
        if statut_filtre:
            query = query.eq("statut", statut_filtre)
        else:
            query = query.neq("statut", "CLOSED")
        result = query.order("date_identification", desc=True).limit(10).execute()
        risques = result.data if result.data else []

        if not risques:
            label = f"statut {statut_filtre}" if statut_filtre else "ouverts"
            envoyer_whatsapp(expediteur, f"ℹ️ Aucun risque {label}.", message_id)
            return True

        label = statut_filtre or "OUVERTS"
        msg = f"━━━━━━━━━━━━━━━━━━━━━━\n📋 RISQUES {label} ({len(risques)})\n━━━━━━━━━━━━━━━━━━━━━━\n\n"

        emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}
        for i, r in enumerate(risques, 1):
            ep = emoji_priorite.get(r.get('priorite'), "⚪")
            msg += (
                f"{i}. *{r['risque_id']}*\n"
                f"   📁 {r.get('type_projet','?')} — 📍 {str(r.get('site','?')).upper()}\n"
                f"   {ep} {r.get('priorite','?')} | {r.get('score_global','?')}/25 | {r.get('statut','?')}\n\n"
            )

        msg += f"🌐 Dashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg, message_id)
        return True
    except Exception as e:
        print(f"==> Erreur liste risques: {e}")
        envoyer_whatsapp(expediteur, "❌ Erreur récupération.", message_id)
        return True

def envoyer_risques_par_site(expediteur, site, message_id=None):
    supabase = get_supabase()
    if not supabase:
        envoyer_whatsapp(expediteur, "❌ Base non disponible.", message_id)
        return True
    try:
        result = supabase.table("risques").select("*").ilike("site", f"%{site}%").order(
            "date_identification", desc=True).limit(10).execute()
        risques = result.data if result.data else []

        if not risques:
            envoyer_whatsapp(expediteur, f"ℹ️ Aucun risque pour le site *{site.upper()}*.", message_id)
            return True

        msg = f"━━━━━━━━━━━━━━━━━━━━━━\n📋 RISQUES SITE {site.upper()} ({len(risques)})\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}
        for i, r in enumerate(risques, 1):
            ep = emoji_priorite.get(r.get('priorite'), "⚪")
            msg += (
                f"{i}. *{r['risque_id']}*\n"
                f"   {ep} {r.get('priorite','?')} | {r.get('statut','?')}\n"
                f"   📝 {str(r.get('description_risque','?'))[:60]}...\n\n"
            )
        msg += f"🌐 Dashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg, message_id)
        return True
    except Exception as e:
        print(f"==> Erreur risques site: {e}")
        envoyer_whatsapp(expediteur, "❌ Erreur récupération.", message_id)
        return True

# Gestion de la session CLOSE avec raison
def traiter_close_avec_raison(expediteur, message, message_id):
    conv = get_conversation(expediteur)
    if not conv or conv.get("etape") != "close_reason":
        return False

    data = conv.get("data", {})
    risque_id = data.get("risque_id")
    raison = message.strip()

    if len(raison) < 5:
        envoyer_whatsapp(expediteur,
            "⚠️ Veuillez décrire l'ACTION MENEE plus en détail.", message_id)
        return True

    risque = recuperer_risque_par_id(risque_id)
    mettre_a_jour_risque(risque_id, {
        "statut": "CLOSED",
        "reponse_apportee": raison,
        "owner_contact": expediteur,
        "date_resolution": datetime.now().isoformat()
    })

    delete_conversation(expediteur)

    envoyer_whatsapp(expediteur,
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚫ RISQUE FERME\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 ID : *{risque_id}*\n"
        f"📍 SITE : {str(risque.get('site','?')).upper()}\n"
        f"📝 ACTION MENEE : {raison}\n"
        f"🕐 DATE : {datetime.now().strftime('%d/%m/%Y à %H:%M')}", message_id)

    # Notifier le signalant
    signalant = risque.get("source_stakeholder_contact") if risque else None
    msg_id_signalant = risque.get("message_id_signalant") if risque else None
    if signalant:
        envoyer_whatsapp(signalant,
            f"✅ Votre signalement *{risque_id}* a été fermé.\n\n"
            f"📝 ACTION MENEE : {raison}\n"
            f"🕐 {datetime.now().strftime('%d/%m/%Y à %H:%M')}\n\n"
            f"Merci pour votre vigilance.",
            msg_id_signalant)
    return True

# ============================================================
# RECHERCHE PAR PERIODE
# ============================================================

def traiter_recherche_periode(expediteur, message, message_id=None):
    msg_upper = message.strip().upper()

    mois_map = {
        'JANVIER': 1, 'JAN': 1, 'FEVRIER': 2, 'FEV': 2,
        'MARS': 3, 'MAR': 3, 'AVRIL': 4, 'AVR': 4,
        'MAI': 5, 'JUIN': 6, 'JUI': 6, 'JUILLET': 7,
        'AOUT': 8, 'AOU': 8, 'SEPTEMBRE': 9, 'SEP': 9,
        'OCTOBRE': 10, 'OCT': 10, 'NOVEMBRE': 11, 'NOV': 11,
        'DECEMBRE': 12, 'DEC': 12
    }

    match_liste = re.match(r'^LISTE\s+([A-ZÉÈÊ]+)(?:\s+(\d{4}))?$', msg_upper)
    if match_liste:
        mois_str = match_liste.group(1)
        annee_str = match_liste.group(2)
        if mois_str.isdigit():
            return envoyer_liste_annee(expediteur, int(mois_str), message_id)
        mois = mois_map.get(mois_str)
        if not mois:
            envoyer_whatsapp(expediteur, "⚠️ Mois non reconnu.", message_id)
            return True
        annee = int(annee_str) if annee_str else datetime.now().year
        return envoyer_liste_mois(expediteur, annee, mois, message_id)

    match_stats = re.match(r'^STATS\s+([A-ZÉÈÊ]+)(?:\s+(\d{4}))?$', msg_upper)
    if match_stats:
        mois_str = match_stats.group(1)
        annee_str = match_stats.group(2)
        if mois_str.isdigit():
            envoyer_whatsapp(expediteur, "Pour stats annuelles: STATS 2026", message_id)
            return True
        mois = mois_map.get(mois_str)
        if not mois:
            envoyer_whatsapp(expediteur, "⚠️ Mois non reconnu.", message_id)
            return True
        annee = int(annee_str) if annee_str else datetime.now().year
        return envoyer_stats_mois(expediteur, annee, mois, message_id)

    return False

def envoyer_liste_mois(expediteur, annee, mois, message_id=None):
    supabase = get_supabase()
    if not supabase:
        envoyer_whatsapp(expediteur, "❌ Base non disponible.", message_id)
        return True
    aa = str(annee)[-2:]
    mm = f"{mois:02d}"
    try:
        result = supabase.table("risques").select("*").like("risque_id", f"___{aa}{mm}%").execute()
        risques = result.data if result.data else []
        if not risques:
            envoyer_whatsapp(expediteur, f"ℹ️ Aucun risque pour {mois:02d}/{annee}.", message_id)
            return True
        msg = f"━━━━━━━━━━━━━━━━━━━━━━\n📋 RISQUES {mois:02d}/{annee} ({len(risques)})\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}
        for i, r in enumerate(risques[:10], 1):
            ep = emoji_priorite.get(r.get('priorite'), "⚪")
            msg += (
                f"{i}. *{r['risque_id']}*\n"
                f"   📁 {r.get('type_projet','?')} — 📍 {str(r.get('site','?')).upper()}\n"
                f"   {ep} {r.get('priorite','?')} | {r.get('score_global','?')}/25\n\n"
            )
        if len(risques) > 10:
            msg += f"... et {len(risques) - 10} autres.\n\n"
        msg += f"🌐 Dashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg, message_id)
        return True
    except Exception as e:
        print(f"==> Erreur liste mois: {e}")
        envoyer_whatsapp(expediteur, "❌ Erreur récupération.", message_id)
        return True

def envoyer_liste_annee(expediteur, annee, message_id=None):
    supabase = get_supabase()
    if not supabase:
        envoyer_whatsapp(expediteur, "❌ Base non disponible.", message_id)
        return True
    try:
        aa = str(annee)[-2:]
        result = supabase.table("risques").select("*").like("risque_id", f"___{aa}%").execute()
        risques = result.data if result.data else []
        if not risques:
            envoyer_whatsapp(expediteur, f"ℹ️ Aucun risque pour {annee}.", message_id)
            return True
        par_mois = {}
        for r in risques:
            mm = r['risque_id'][6:8]
            par_mois.setdefault(mm, []).append(r)
        msg = f"━━━━━━━━━━━━━━━━━━━━━━\n📋 RISQUES {annee} ({len(risques)})\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for mm in sorted(par_mois.keys()):
            mois_risques = par_mois[mm]
            critiques = sum(1 for r in mois_risques if r.get('priorite') == 'CRITIQUE')
            msg += f"📅 Mois {mm}: {len(mois_risques)} risque(s) ({critiques} 🔴 critiques)\n"
        msg += f"\n🌐 Dashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg, message_id)
        return True
    except Exception as e:
        print(f"==> Erreur liste annee: {e}")
        envoyer_whatsapp(expediteur, "❌ Erreur récupération.", message_id)
        return True

def envoyer_stats_mois(expediteur, annee, mois, message_id=None):
    supabase = get_supabase()
    if not supabase:
        envoyer_whatsapp(expediteur, "❌ Base non disponible.", message_id)
        return True
    try:
        result = supabase.rpc("stats_periode", {"annee": annee, "mois": mois}).execute()
        stats = result.data[0] if result.data else None
        if not stats:
            envoyer_whatsapp(expediteur, f"ℹ️ Stats non disponibles pour {mois:02d}/{annee}.", message_id)
            return True
        msg = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 STATS {mois:02d}/{annee}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📈 TOTAL : {stats.get('total', 0)}\n"
            f"🚧 BLOQUANTS : {stats.get('bloquants', 0)}\n"
            f"✅ FERMES : {stats.get('fermes', 0)}\n"
            f"📊 SCORE MOYEN : {stats.get('moyenne_score', 0):.2f}/25\n\n"
            f"PAR PRIORITE :\n"
        )
        par_prio = stats.get('par_priorite', {})
        for prio, emoji in [('CRITIQUE','🔴'), ('ELEVE','🟠'), ('MOYEN','🟡'), ('FAIBLE','🟢')]:
            if prio in par_prio:
                msg += f"  {emoji} {prio}: {par_prio[prio]}\n"
        msg += f"\n🌐 Dashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg, message_id)
        return True
    except Exception as e:
        print(f"==> Erreur stats mois: {e}")
        envoyer_whatsapp(expediteur, "❌ Erreur stats.", message_id)
        return True

# ============================================================
# RAPPORT HEBDO
# ============================================================

def generer_rapport_hebdo():
    supabase = get_supabase()
    if not supabase:
        return
    try:
        debut = (datetime.now() - timedelta(days=7)).isoformat()
        result = supabase.table("risques").select("*").gte("date_identification", debut).execute()
        risques = result.data
        total = len(risques)
        par_priorite = {}
        par_type_projet = {}
        moy_score = 0
        bloquants = 0
        for r in risques:
            p = r.get("priorite", "MOYEN")
            par_priorite[p] = par_priorite.get(p, 0) + 1
            tp = r.get("type_projet", "AUTRES")
            par_type_projet[tp] = par_type_projet.get(tp, 0) + 1
            moy_score += r.get("score_global", 0) or 0
            if r.get("bloque_projet"):
                bloquants += 1
        moy_score = moy_score / total if total else 0

        prompt = f"""Tu es un expert en gestion des risques télécoms en Côte d'Ivoire.
Rédige un rapport hebdomadaire professionnel en FRANÇAIS PARFAIT pour WhatsApp.

DONNÉES SEMAINE {datetime.now().isocalendar()[1]}:
- Total risques: {total}
- Par priorité: {json.dumps(par_priorite, ensure_ascii=False)}
- Par projet: {json.dumps(par_type_projet, ensure_ascii=False)}
- Score moyen: {moy_score:.1f}/25
- Risques bloquants: {bloquants}

Structure du rapport:
1. RESUME EXECUTIF (2 phrases)
2. CHIFFRES CLES
3. TOP 3 POINTS D'ATTENTION
4. TENDANCES OBSERVEES
5. SCORE DE SANTE GLOBAL (0-100 avec explication)
6. DECISIONS URGENTES REQUISES"""

        groq_client = get_groq_client()
        if groq_client:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                timeout=20
            )
            rapport = response.choices[0].message.content
        else:
            rapport = f"Rapport semaine {datetime.now().isocalendar()[1]}\n\nTotal: {total} risques\nScore moyen: {moy_score:.1f}/25\nBloquants: {bloquants}"

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
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 RAPPORT HEBDO — S{datetime.now().isocalendar()[1]}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{rapport}\n\n"
                    f"🌐 Dashboard: {LIEN_DASHBOARD}")
    except Exception as e:
        print(f"==> Erreur rapport: {e}")

# ============================================================
# ESCALADE TEMPORELLE
# ============================================================

def verifier_escalades_en_attente():
    supabase = get_supabase()
    if not supabase:
        return
    try:
        result = supabase.table("risques").select("*").eq("statut", "OPENED").lte(
            "date_identification",
            (datetime.now() - timedelta(minutes=15)).isoformat()
        ).execute()
        for alerte in result.data:
            priorite = alerte.get("priorite", "MOYEN")
            cree = datetime.fromisoformat(alerte["date_identification"].replace("Z", "+00:00"))
            minutes = (datetime.now() - cree.replace(tzinfo=None)).total_seconds() / 60
            regles = {
                "CRITIQUE": (15, "CHEF_PROJET", "PMO"),
                "ELEVE":    (30, "CHEF_PROJET", "DIRECTEUR"),
                "MOYEN":    (60, "CHEF_PROJET", "DIRECTEUR")
            }
            if priorite in regles:
                delai, _, niveau2 = regles[priorite]
                if minutes > delai and alerte.get("niveau_escalade", 0) < 1:
                    astreinte = get_astreinte(niveau2)
                    if astreinte and astreinte.get("telephone"):
                        envoyer_whatsapp(astreinte["telephone"],
                            f"⏰ ESCALADE [{priorite}] — {delai}min sans action\n\n"
                            f"🆔 ID : *{alerte['risque_id']}*\n"
                            f"🏗️ PROJET : {str(alerte.get('nom_projet','?')).upper()}\n"
                            f"📍 SITE : {str(alerte.get('site','?')).upper()}\n"
                            f"📋 DESC : {alerte.get('description_risque','?')}\n"
                            f"👤 SIGNALE PAR : {str(alerte.get('source_stakeholder_nom_prenom', alerte.get('source_stakeholder_contact','?'))).upper()}\n\n"
                            f"🌐 Dashboard: {LIEN_DASHBOARD}")
                        mettre_a_jour_risque(alerte["risque_id"], {"niveau_escalade": 1})
    except Exception as e:
        print(f"==> Erreur escalade: {e}")

# ============================================================
# ROUTES FLASK
# ============================================================

@app.route("/")
def home():
    return "DigitalRiskPlatform v5.0 — 5 etapes + UPDATE + ASSIGN + IA enrichie", 200

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
                        supabase = get_supabase()
                        if supabase:
                            result = supabase.table("risques").select("risque_id").eq("message_id_whatsapp", msg_id).execute()
                            if result.data:
                                rid = result.data[0]["risque_id"]
                                mettre_a_jour_risque(rid, {"statut": "ASSIGNED", "owner_contact": expediteur})
                                envoyer_whatsapp(expediteur, f"✅ Risque *{rid}* — Pris en charge.", msg_id)
                        return jsonify({"status": "pris_en_charge"})
                    elif emoji in ["⬆️", "🔴", "⚠️"]:
                        envoyer_whatsapp(expediteur, "⬆️ Escalade notée.", msg_id)
                        return jsonify({"status": "escalade"})

        if "messages" not in value:
            return jsonify({"status": "ok"})

        msg_data = value["messages"][0]
        msg_id = msg_data["id"]
        msg_type = msg_data.get("type", "text")
        if msg_type != "text":
            return jsonify({"status": "ok"})

        if message_deja_traite(msg_id):
            print(f"==> Message duplique ignore: {msg_id}")
            return jsonify({"status": "ok"})
        marquer_message_traite(msg_id)

        message = msg_data["text"]["body"]
        expediteur = msg_data["from"]
        print(f"==> Message: {message} | De: {expediteur}")

        # Session CLOSE en attente de raison
        if traiter_close_avec_raison(expediteur, message, msg_id):
            return jsonify({"status": "close_raison"})

        # Commandes manager
        if traiter_commande_manager(expediteur, message, msg_id):
            return jsonify({"status": "commande_manager"})

        # Recherche période
        if traiter_recherche_periode(expediteur, message, msg_id):
            return jsonify({"status": "recherche_periode"})

        # Commandes générales
        cmd = message.strip().upper()
        if cmd in ["RAPPORT", "DASHBOARD"]:
            envoyer_whatsapp(expediteur, f"🌐 Dashboard: {LIEN_DASHBOARD}", msg_id)
            return jsonify({"status": "ok"})

        if cmd in ["AIDE", "HELP", "MENU"]:
            envoyer_whatsapp(expediteur,
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📱 DIGITALRISKPLATFORM\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "📋 SIGNALER UN RISQUE:\n"
                "Envoyez *#* pour commencer\n\n"
                "📊 RECHERCHE:\n"
                "LISTE MAI [2026] — Risques du mois\n"
                "LISTE 2026 — Risques de l'année\n"
                "STATS MAI [2026] — Statistiques\n\n"
                "👤 MES RISQUES:\n"
                "MES-RISQUES — Tous les risques ouverts\n"
                "MES-RISQUES OPENED — Par statut\n"
                "RISQUES-SITE COCODY — Par site\n\n"
                "🔧 MANAGERS:\n"
                "STATUS XXXAAMM0000 — Voir statut\n"
                "CLOSE XXXAAMM0000 — Fermer\n"
                "UPDATE XXXAAMM0000 IN_PROGRESS action — Mettre à jour\n"
                "ASSIGN XXXAAMM0000 +225XXXXXXXXX — Assigner\n\n"
                "📊 STATUTS: OPENED | ASSIGNED | IN_PROGRESS | RESOLVED | CLOSED\n\n"
                "🌐 RAPPORT — Dashboard", msg_id)
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
# ROUTES DE TEST
# ============================================================

@app.route("/test-db")
def test_db():
    supabase = get_supabase()
    if not supabase:
        return "Supabase non configure", 500
    try:
        result = supabase.table("risques").select("count", count="exact").execute()
        count = result.count if hasattr(result, 'count') else "OK"
        return f"Connexion OK - Risques: {count}", 200
    except Exception as e:
        return f"Erreur: {str(e)}", 500

@app.route("/cron/escalades")
def cron_escalades():
    verifier_escalades_en_attente()
    return "Escalades verifiees", 200

@app.route("/cron/rapport-hebdo")
def cron_rapport_hebdo():
    generer_rapport_hebdo()
    return "Rapport genere", 200

# ============================================================
# DEMARRAGE
# ============================================================

def demarrer_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(verifier_escalades_en_attente, 'interval', minutes=5,
                      id='escalades', replace_existing=True)
    scheduler.add_job(generer_rapport_hebdo, 'cron', day_of_week='mon',
                      hour=8, minute=0, id='rapport_hebdo', replace_existing=True)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    print("==> Scheduler demarre : escalades (5min) + rapport hebdo (lundi 8h)")
    return scheduler

if __name__ == "__main__":
    demarrer_scheduler()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
