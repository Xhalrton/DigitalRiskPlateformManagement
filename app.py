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

# SUPERADMIN : seul contact prédéfini en dur. Tous les autres sont en base.
SUPERADMIN_PHONE = os.environ.get("SUPERADMIN_PHONE", "+2250500000000")

LIEN_DASHBOARD = os.environ.get("DASHBOARD_URL", "https://google.com")

# Délai de validation inscription (en minutes)
DELAI_VALIDATION_INSCRIPTION = 5

TYPES_PROJET = ["RAN", "RURAL", "FIBRE", "CORE", "IPRAN", "MWV", "MMONEY", "HOME", "AUTRES"]
TYPES_RISQUE = ["ACCES", "SECURITE", "TECHNIQUE", "ADMINISTRATIF", "METEO", "LOGISTIQUE", "SANITAIRE", "SOCIAL", "AUTRES"]
STATUTS_VALIDES = ["OPENED", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED"]

# Positions disponibles à l'inscription
POSITIONS = {
    "1": "DIRECTEUR_PROJET",
    "2": "PMO",
    "3": "CONSULTANT_EXTERNE",
    "4": "CHEF_DE_PROJET",
    "5": "COORDINATEUR_PROJET",
    "6": "SUPERVISEUR",
    "7": "TEAM_LEADER",
    "8": "TECHNICIEN",
    "9": "AUTRE_STAKEHOLDER"
}

# Rôles système
ROLES = {
    "1": "SUPERADMIN",
    "2": "ADMIN",
    "3": "UTILISATEUR"
}

# Niveaux d'escalade selon priorité (dynamique, récupéré depuis la base)
ESCALADE_PRIORITE = {
    "FAIBLE":   [],
    "MOYEN":    ["CHEF_DE_PROJET"],
    "ELEVE":    ["CHEF_DE_PROJET", "PMO"],
    "CRITIQUE": ["CHEF_DE_PROJET", "PMO", "DIRECTEUR_PROJET"]
}

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

# ============================================================
# GESTION DES UTILISATEURS — Inscription & Rôles dynamiques
# ============================================================

def get_utilisateur(telephone):
    """Retourne l'utilisateur depuis la base, ou None s'il n'existe pas."""
    supabase = get_supabase()
    if not supabase:
        return None
    try:
        result = supabase.table("utilisateurs").select("*").eq("telephone", telephone).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        print(f"==> Erreur get_utilisateur: {e}")
        return None

def creer_utilisateur(telephone, nom_prenom, position, role="UTILISATEUR", actif=False, valide=False):
    """Crée un utilisateur en base."""
    supabase = get_supabase()
    if not supabase:
        return False
    try:
        supabase.table("utilisateurs").insert({
            "telephone": telephone,
            "nom_prenom": nom_prenom,
            "position": position,
            "role": role,
            "actif": actif,
            "valide": valide,
            "date_inscription": datetime.now().isoformat()
        }).execute()
        return True
    except Exception as e:
        print(f"==> Erreur creer_utilisateur: {e}")
        return False

def mettre_a_jour_utilisateur(telephone, updates):
    """Met à jour un utilisateur en base."""
    supabase = get_supabase()
    if not supabase:
        return False
    try:
        supabase.table("utilisateurs").update(updates).eq("telephone", telephone).execute()
        return True
    except Exception as e:
        print(f"==> Erreur MAJ utilisateur: {e}")
        return False

def est_superadmin(telephone):
    """Vérifie si le numéro est SuperAdmin (fixe en dur OU en base)."""
    if telephone == SUPERADMIN_PHONE:
        return True
    user = get_utilisateur(telephone)
    return user and user.get("role") == "SUPERADMIN"

def est_admin(telephone):
    """Vérifie si le numéro est Admin ou SuperAdmin."""
    if est_superadmin(telephone):
        return True
    user = get_utilisateur(telephone)
    return user and user.get("role") in ["ADMIN", "SUPERADMIN"]

def get_role_utilisateur_info(telephone):
    """Retourne {role, nom_prenom, position} ou None."""
    if telephone == SUPERADMIN_PHONE:
        user = get_utilisateur(telephone)
        if user:
            return {"role": "SUPERADMIN", "nom_prenom": user.get("nom_prenom", "SuperAdmin"), "position": user.get("position", "SUPERADMIN")}
        return {"role": "SUPERADMIN", "nom_prenom": "SuperAdmin", "position": "SUPERADMIN"}
    user = get_utilisateur(telephone)
    if not user:
        return None
    return {
        "role": user.get("role", "UTILISATEUR"),
        "nom_prenom": user.get("nom_prenom", ""),
        "position": user.get("position", ""),
        "actif": user.get("actif", False),
        "valide": user.get("valide", False)
    }

def get_admins_actifs():
    """Retourne la liste des admins actifs (pour notifications de validation)."""
    supabase = get_supabase()
    admins = []
    # Toujours inclure le SuperAdmin fixe
    if SUPERADMIN_PHONE:
        admins.append({"telephone": SUPERADMIN_PHONE, "nom_prenom": "SuperAdmin"})
    if not supabase:
        return admins
    try:
        result = supabase.table("utilisateurs").select("telephone, nom_prenom").in_(
            "role", ["SUPERADMIN", "ADMIN"]
        ).eq("actif", True).execute()
        for u in result.data:
            if u["telephone"] != SUPERADMIN_PHONE:  # Eviter doublon
                admins.append(u)
    except Exception as e:
        print(f"==> Erreur get_admins_actifs: {e}")
    return admins

# Fonction supprimée : validation automatique — les comptes sont activés immédiatement à l'inscription

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
# GENERATION ID RISQUE
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
# SUPABASE — RISQUES
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

def get_astreinte(position):
    """Récupère le contact actif pour une position donnée (ex: CHEF_DE_PROJET)."""
    supabase = get_supabase()
    if not supabase:
        return None
    try:
        # D'abord chercher dans la table astreintes si elle existe
        try:
            result = supabase.table("astreintes").select(
                "utilisateurs(telephone, nom_prenom)"
            ).eq("role", position).eq("actif", True).lte(
                "date_debut", datetime.now().isoformat()
            ).gte("date_fin", datetime.now().isoformat()).execute()
            if result.data and result.data[0].get("utilisateurs"):
                u = result.data[0]["utilisateurs"]
                return {"telephone": u["telephone"], "nom": u.get("nom_prenom", position)}
        except Exception:
            pass
        # Fallback : chercher par position dans utilisateurs
        result = supabase.table("utilisateurs").select("telephone, nom_prenom").eq(
            "position", position
        ).eq("actif", True).execute()
        if result.data:
            return {"telephone": result.data[0]["telephone"], "nom": result.data[0].get("nom_prenom", position)}
    except Exception as e:
        print(f"==> Erreur astreinte: {e}")
    return None

# ============================================================
# WHATSAPP
# ============================================================

def envoyer_whatsapp(numero, message, message_id_reference=None):
    if WA_TOKEN == "placeholder" or not WA_TOKEN:
        print(f"==> [SIMULATION] WhatsApp a {numero}: {message[:80]}...")
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
# INSCRIPTION UTILISATEUR — Flux multi-étapes
# Etape REG_1: Nom & Prénom
# Etape REG_2: Position (1-9)
# Etape REG_3: Confirmation
# ============================================================

def demarrer_inscription(expediteur, message_id=None):
    set_conversation(expediteur, "REG_1", {})
    envoyer_whatsapp(expediteur,
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👋 BIENVENUE — INSCRIPTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Vous n'êtes pas encore inscrit(e).\n"
        "Quelques secondes pour créer votre profil.\n\n"
        "📋 ETAPE 1/2 — NOM & PRENOM\n\n"
        "Indiquez votre NOM et PRENOM complets.\n\n"
        "Exemple: KOUASSI Jean-Baptiste", message_id)

def traiter_inscription(expediteur, message, message_id):
    """Gère les étapes d'inscription d'un nouvel utilisateur."""
    conv = get_conversation(expediteur)
    if not conv:
        return False

    etape = conv.get("etape", "")
    if not str(etape).startswith("REG_"):
        return False

    data = conv.get("data", {})
    msg_upper = message.strip().upper()

    # Annulation possible
    if msg_upper in ["ANNULER", "CANCEL", "RESET"]:
        delete_conversation(expediteur)
        envoyer_whatsapp(expediteur, "❌ Inscription annulée.\n\nRenvoyez n'importe quel message pour recommencer.", message_id)
        return True

    if etape == "REG_1":
        nom = message.strip()
        if len(nom) < 3:
            envoyer_whatsapp(expediteur,
                "⚠️ Veuillez indiquer votre NOM et PRENOM complets.\n\nExemple: KOUASSI Jean-Baptiste", message_id)
            return True
        data["nom_prenom"] = nom
        set_conversation(expediteur, "REG_2", data)
        envoyer_whatsapp(expediteur,
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 2/2 — POSITION\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choisissez votre position:\n\n"
            "1. DIRECTEUR PROJET\n"
            "2. PMO\n"
            "3. CONSULTANT EXTERNE\n"
            "4. CHEF DE PROJET\n"
            "5. COORDINATEUR PROJET\n"
            "6. SUPERVISEUR\n"
            "7. TEAM LEADER\n"
            "8. TECHNICIEN\n"
            "9. AUTRE STAKEHOLDER\n\n"
            "Répondez par le NUMERO (1 à 9).", message_id)
        return True

    elif etape == "REG_2":
        choix = message.strip()
        position = POSITIONS.get(choix)
        if not position:
            envoyer_whatsapp(expediteur,
                "⚠️ Choix non reconnu. Répondez par un numéro de 1 à 9.", message_id)
            return True
        data["position"] = position
        set_conversation(expediteur, "REG_3", data)
        label_position = choix + ". " + position.replace("_", " ")
        envoyer_whatsapp(expediteur,
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ RESUME INSCRIPTION\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 NOM : {data['nom_prenom'].upper()}\n"
            f"💼 POSITION : {position.replace('_', ' ')}\n"
            f"📞 TELEPHONE : {expediteur}\n\n"
            "Envoyez *OK* pour confirmer\n"
            "ou *!* pour corriger la position\n"
            "ou *ANNULER* pour tout recommencer.", message_id)
        return True

    elif etape == "REG_3":
        if msg_upper == "!":
            # Retour étape précédente
            set_conversation(expediteur, "REG_2", data)
            envoyer_whatsapp(expediteur,
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📋 ETAPE 2/2 — POSITION\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Choisissez votre position:\n\n"
                "1. DIRECTEUR PROJET\n"
                "2. PMO\n"
                "3. CONSULTANT EXTERNE\n"
                "4. CHEF DE PROJET\n"
                "5. COORDINATEUR PROJET\n"
                "6. SUPERVISEUR\n"
                "7. TEAM LEADER\n"
                "8. TECHNICIEN\n"
                "9. AUTRE STAKEHOLDER\n\n"
                "Répondez par le NUMERO (1 à 9).", message_id)
            return True

        if msg_upper in ["OK", "OUI", "VALIDER"]:
            # Enregistrer avec actif=False en attente de validation
            success = creer_utilisateur(
                telephone=expediteur,
                nom_prenom=data["nom_prenom"],
                position=data["position"],
                role="UTILISATEUR",
                actif=True,
                valide=True
            )
            delete_conversation(expediteur)

            if not success:
                envoyer_whatsapp(expediteur,
                    "❌ Erreur lors de l'inscription. Veuillez réessayer.", message_id)
                return True

            envoyer_whatsapp(expediteur,
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ INSCRIPTION REUSSIE\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 {data['nom_prenom'].upper()}\n"
                f"💼 {data['position'].replace('_', ' ')}\n\n"
                "Votre compte est activé !\n\n"
                "Envoyez *#* pour signaler un risque.\n"
                "Tapez *AIDE* pour voir toutes les commandes.", message_id)

            # Notifier les admins (info seulement, pas de validation requise)
            admins = get_admins_actifs()
            for admin in admins:
                if admin["telephone"] != expediteur:
                    envoyer_whatsapp(admin["telephone"],
                        f"ℹ️ Nouvel utilisateur inscrit\n\n"
                        f"👤 {data['nom_prenom'].upper()}\n"
                        f"💼 {data['position'].replace('_', ' ')}\n"
                        f"📞 {expediteur}\n\n"
                        f"Rôle par défaut : UTILISATEUR\n"
                        f"Pour modifier : *ROLE {expediteur} 2*")
            return True
        else:
            envoyer_whatsapp(expediteur,
                "Répondez *OK* pour confirmer, *!* pour retour, *ANNULER* pour tout arrêter.", message_id)
            return True

    return False

# ============================================================
# COMMANDES ADMIN — Validation inscriptions & gestion rôles
# ============================================================

def traiter_commandes_admin(expediteur, message, message_id=None):
    """Traite les commandes réservées aux admins et superadmins."""
    msg_upper = message.strip().upper()
    msg_original = message.strip()

    if not est_admin(expediteur):
        return False

    # --- VALIDER inscription ---
    # Format: VALIDER +2250XXXXXXXXX [1|2|3]
    match_valider = re.match(r'^VALIDER\s+(\+?\d{10,15})(?:\s+([123]))?$', msg_upper)
    if match_valider:
        telephone_cible = match_valider.group(1)
        # Normaliser : supprimer le + si présent (stockage sans +)
        telephone_cible = telephone_cible.lstrip("+")
        role_num = match_valider.group(2) or "3"
        role_attribue = ROLES.get(role_num, "UTILISATEUR")

        # Restrictions : seul le SuperAdmin peut créer un autre SuperAdmin
        if role_attribue == "SUPERADMIN" and not est_superadmin(expediteur):
            envoyer_whatsapp(expediteur,
                "❌ Seul le *SUPERADMIN* peut attribuer ce rôle.", message_id)
            return True

        # Max 2 Admins (SuperAdmin inclus) — vérification
        if role_attribue == "ADMIN":
            supabase = get_supabase()
            if supabase:
                try:
                    count_result = supabase.table("utilisateurs").select("telephone").in_(
                        "role", ["ADMIN", "SUPERADMIN"]
                    ).execute()
                    nb_admins = len(count_result.data) if count_result.data else 0
                    # +1 pour le SUPERADMIN_PHONE fixe s'il n'est pas en base
                    if nb_admins >= 2:
                        envoyer_whatsapp(expediteur,
                            "❌ Limite de 2 administrateurs atteinte.\n"
                            "Révoquez un admin existant avant d'en créer un nouveau.", message_id)
                        return True
                except Exception as e:
                    print(f"==> Erreur compte admins: {e}")

        user = get_utilisateur(telephone_cible)
        if not user:
            envoyer_whatsapp(expediteur,
                f"❌ Utilisateur *{telephone_cible}* introuvable.", message_id)
            return True

        mettre_a_jour_utilisateur(telephone_cible, {
            "actif": True,
            "valide": True,
            "role": role_attribue
        })

        envoyer_whatsapp(expediteur,
            f"✅ Compte validé !\n\n"
            f"👤 {user.get('nom_prenom','?').upper()}\n"
            f"🎭 Rôle : *{role_attribue}*\n"
            f"📞 {telephone_cible}", message_id)

        # Notifier l'utilisateur
        envoyer_whatsapp(telephone_cible,
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ COMPTE ACTIVE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Votre compte a été validé !\n"
            f"🎭 Rôle : *{role_attribue}*\n\n"
            "Envoyez *#* pour signaler un risque.\n"
            "Tapez *AIDE* pour voir toutes les commandes.")
        return True

    # --- REFUSER inscription ---
    match_refuser = re.match(r'^REFUSER\s+(\+?\d{10,15})$', msg_upper)
    if match_refuser:
        telephone_cible = match_refuser.group(1)
        # Normaliser : supprimer le + si présent (stockage sans +)
        telephone_cible = telephone_cible.lstrip("+")

        user = get_utilisateur(telephone_cible)
        if not user:
            envoyer_whatsapp(expediteur, f"❌ Utilisateur *{telephone_cible}* introuvable.", message_id)
            return True

        supabase = get_supabase()
        if supabase:
            supabase.table("utilisateurs").delete().eq("telephone", telephone_cible).execute()

        envoyer_whatsapp(expediteur,
            f"🚫 Inscription refusée pour *{telephone_cible}*.", message_id)
        envoyer_whatsapp(telephone_cible,
            "🚫 Votre demande d'inscription a été refusée.\n\n"
            "Pour toute question, contactez votre responsable.")
        return True

    # --- ROLE — modifier le rôle d'un utilisateur ---
    # Format: ROLE +2250XXXXXXXXX [1|2|3]
    match_role = re.match(r'^ROLE\s+(\+?\d{10,15})\s+([123])$', msg_upper)
    if match_role:
        telephone_cible = match_role.group(1)
        # Normaliser : supprimer le + si présent (stockage sans +)
        telephone_cible = telephone_cible.lstrip("+")
        role_num = match_role.group(2)
        nouveau_role = ROLES.get(role_num, "UTILISATEUR")

        # Restrictions hiérarchiques
        # Le 2ème admin ne peut pas révoquer le 1er (SuperAdmin)
        user_cible = get_utilisateur(telephone_cible)
        if not user_cible:
            envoyer_whatsapp(expediteur, f"❌ Utilisateur *{telephone_cible}* introuvable.", message_id)
            return True

        # Protéger le SuperAdmin fixe
        if telephone_cible == SUPERADMIN_PHONE:
            envoyer_whatsapp(expediteur,
                "❌ Le rôle du SuperAdmin principal ne peut pas être modifié.", message_id)
            return True

        # Un ADMIN ne peut pas modifier le rôle d'un SUPERADMIN
        if user_cible.get("role") == "SUPERADMIN" and not est_superadmin(expediteur):
            envoyer_whatsapp(expediteur,
                "❌ Vous ne pouvez pas modifier le rôle d'un SuperAdmin.", message_id)
            return True

        # Seul le SuperAdmin peut attribuer SUPERADMIN ou ADMIN
        if nouveau_role in ["SUPERADMIN", "ADMIN"] and not est_superadmin(expediteur):
            # Vérifier si l'admin actuel est le 2ème admin (ne peut pas créer d'admin)
            envoyer_whatsapp(expediteur,
                "❌ Seul le *SUPERADMIN* peut attribuer les rôles ADMIN et SUPERADMIN.", message_id)
            return True

        # Vérifier limite 2 admins
        if nouveau_role == "ADMIN":
            supabase = get_supabase()
            if supabase:
                try:
                    count_result = supabase.table("utilisateurs").select("telephone").in_(
                        "role", ["ADMIN", "SUPERADMIN"]
                    ).execute()
                    nb_admins = len(count_result.data) if count_result.data else 0
                    if nb_admins >= 2 and user_cible.get("role") not in ["ADMIN", "SUPERADMIN"]:
                        envoyer_whatsapp(expediteur,
                            "❌ Limite de 2 administrateurs atteinte.", message_id)
                        return True
                except Exception:
                    pass

        mettre_a_jour_utilisateur(telephone_cible, {"role": nouveau_role})
        envoyer_whatsapp(expediteur,
            f"✅ Rôle mis à jour !\n\n"
            f"👤 {user_cible.get('nom_prenom','?').upper()}\n"
            f"🎭 Nouveau rôle : *{nouveau_role}*", message_id)
        envoyer_whatsapp(telephone_cible,
            f"ℹ️ Votre rôle a été mis à jour : *{nouveau_role}*\n"
            f"Par : {expediteur}")
        return True

    # --- POSITION — modifier la position d'un utilisateur ---
    # Format: POSITION +2250XXXXXXXXX [1-9]
    match_pos = re.match(r'^POSITION\s+(\+?\d{10,15})\s+([1-9])$', msg_upper)
    if match_pos:
        telephone_cible = match_pos.group(1)
        # Normaliser : supprimer le + si présent (stockage sans +)
        telephone_cible = telephone_cible.lstrip("+")
        pos_num = match_pos.group(2)
        nouvelle_position = POSITIONS.get(pos_num)
        if not nouvelle_position:
            envoyer_whatsapp(expediteur, "⚠️ Position invalide (1-9).", message_id)
            return True

        user_cible = get_utilisateur(telephone_cible)
        if not user_cible:
            envoyer_whatsapp(expediteur, f"❌ Utilisateur *{telephone_cible}* introuvable.", message_id)
            return True

        mettre_a_jour_utilisateur(telephone_cible, {"position": nouvelle_position})
        envoyer_whatsapp(expediteur,
            f"✅ Position mise à jour !\n\n"
            f"👤 {user_cible.get('nom_prenom','?').upper()}\n"
            f"💼 Nouvelle position : *{nouvelle_position.replace('_', ' ')}*", message_id)
        envoyer_whatsapp(telephone_cible,
            f"ℹ️ Votre position a été mise à jour : *{nouvelle_position.replace('_', ' ')}*")
        return True

    # --- UTILISATEURS — liste tous les utilisateurs ---
    if msg_upper == "UTILISATEURS":
        supabase = get_supabase()
        if not supabase:
            envoyer_whatsapp(expediteur, "❌ Base non disponible.", message_id)
            return True
        try:
            result = supabase.table("utilisateurs").select("*").order("date_inscription", desc=True).limit(20).execute()
            users = result.data if result.data else []
            if not users:
                envoyer_whatsapp(expediteur, "ℹ️ Aucun utilisateur enregistré.", message_id)
                return True

            emoji_role = {"SUPERADMIN": "👑", "ADMIN": "🛡️", "UTILISATEUR": "👤"}
            emoji_actif = {True: "🟢", False: "🔴"}
            msg = f"━━━━━━━━━━━━━━━━━━━━━━\n👥 UTILISATEURS ({len(users)})\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            for u in users:
                er = emoji_role.get(u.get("role", "UTILISATEUR"), "👤")
                ea = emoji_actif.get(u.get("actif", False), "🔴")
                msg += (
                    f"{er} {ea} *{u.get('nom_prenom','?').upper()}*\n"
                    f"   💼 {u.get('position','?').replace('_',' ')} | 🎭 {u.get('role','?')}\n"
                    f"   📞 {u.get('telephone','?')}\n\n"
                )
            envoyer_whatsapp(expediteur, msg, message_id)
        except Exception as e:
            envoyer_whatsapp(expediteur, f"❌ Erreur: {e}", message_id)
        return True

    # --- DESACTIVER un utilisateur ---
    match_deact = re.match(r'^DESACTIVER\s+(\+?\d{10,15})$', msg_upper)
    if match_deact:
        telephone_cible = match_deact.group(1)
        # Normaliser : supprimer le + si présent (stockage sans +)
        telephone_cible = telephone_cible.lstrip("+")

        if telephone_cible == SUPERADMIN_PHONE:
            envoyer_whatsapp(expediteur, "❌ Impossible de désactiver le SuperAdmin principal.", message_id)
            return True

        user_cible = get_utilisateur(telephone_cible)
        if not user_cible:
            envoyer_whatsapp(expediteur, f"❌ Utilisateur *{telephone_cible}* introuvable.", message_id)
            return True

        # 2ème admin ne peut pas désactiver le 1er
        if user_cible.get("role") in ["SUPERADMIN"] and not est_superadmin(expediteur):
            envoyer_whatsapp(expediteur, "❌ Vous ne pouvez pas désactiver un SuperAdmin.", message_id)
            return True

        mettre_a_jour_utilisateur(telephone_cible, {"actif": False})
        envoyer_whatsapp(expediteur,
            f"✅ Utilisateur *{user_cible.get('nom_prenom','?').upper()}* désactivé.", message_id)
        envoyer_whatsapp(telephone_cible,
            "ℹ️ Votre compte a été désactivé. Contactez votre administrateur.")
        return True

    return False

# ============================================================
# ANALYSE IA
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

Ne renvoie que le JSON, rien d'autre"""

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
# FORMULAIRE SIGNALEMENT — 5 ETAPES avec navigation "!"
# Etape 1: NOM & PRENOM (pré-rempli si utilisateur connu)
# Etape 2: Type de projet
# Etape 3: NOM DU PROJET
# Etape 4: SITE
# Etape 5: DESCRIPTION
# Etape 6: Confirmation
# "!" = étape précédente à tout moment
# ============================================================

ETAPES_PRECEDENTES = {
    2: 1, 3: 2, 4: 3, 5: 4, 6: 5
}

def envoyer_formulaire_etape(numero, etape, data=None, message_id=None):
    messages = {
        1: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 1/5 — IDENTITE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Indiquez votre NOM et PRENOM.\n\n"
            "Exemple: KOUASSI Jean-Baptiste\n\n"
            "_(Tapez *!* pour annuler)_"
        ),
        2: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 2/5 — TYPE DE PROJET\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1. RAN\n2. RURAL\n3. FIBRE\n4. CORE\n5. IPRAN\n"
            "6. MWV\n7. MMONEY\n8. HOME\n9. AUTRES\n\n"
            "Répondez par le NUMERO ou le NOM.\n\n"
            "_(Tapez *!* pour revenir à l'étape précédente)_"
        ),
        3: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 3/5 — NOM DU PROJET\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Indiquez le NOM PRECIS du projet.\n\n"
            "Exemple: DEPLOIEMENT RAN ABIDJAN NORD\n\n"
            "_(Tapez *!* pour revenir à l'étape précédente)_"
        ),
        4: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 4/5 — SITE CONCERNE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Indiquez le NOM ou CODE du site.\n\n"
            "Exemple: COCODY, ABJ_COC_001\n\n"
            "_(Tapez *!* pour revenir à l'étape précédente)_"
        ),
        5: (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 ETAPE 5/5 — DESCRIPTION DU RISQUE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Décrivez le risque en détail:\n\n"
            "• QUEL EST LE PROBLEME ?\n"
            "• QUAND A-T-IL COMMENCE ?\n"
            "• PERSONNES EN DANGER ?\n"
            "• TRAVAUX BLOQUES ?\n\n"
            "Soyez précis — l'IA analysera votre message.\n\n"
            "_(Tapez *!* pour revenir à l'étape précédente)_"
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
            "ou *!* pour modifier la description\n"
            "ou *ANNULER* pour recommencer."
        )
    else:
        msg = messages.get(etape, "Etape inconnue.")

    envoyer_whatsapp(numero, msg, message_id)

def traiter_etape_conversation(expediteur, message, message_id):
    msg_upper = message.strip().upper()
    conv = get_conversation(expediteur)

    # ---- NOUVEAU SIGNALEMENT ----
    if not conv:
        if not msg_upper.startswith("#"):
            envoyer_whatsapp(expediteur,
                "Pour signaler un risque, envoyez *#* pour commencer.", message_id)
            return True

        # Pré-remplir le nom si l'utilisateur est connu
        user_info = get_role_utilisateur_info(expediteur)
        print(f"==> DEBUG # recu - user_info: {user_info}")
        initial_data = {}
        if user_info and user_info.get("nom_prenom"):
            initial_data["nom_signalant"] = user_info["nom_prenom"]
            # Passer directement à l'étape 2 si nom connu
            set_conversation(expediteur, 2, initial_data)
            envoyer_whatsapp(expediteur,
                f"👋 Bonjour *{user_info['nom_prenom'].upper()}* !\n\n"
                "Votre nom a été pré-rempli.\n"
                "Passons directement au type de projet.", message_id)
            envoyer_formulaire_etape(expediteur, 2, message_id=message_id)
        else:
            set_conversation(expediteur, 1, initial_data)
            envoyer_formulaire_etape(expediteur, 1, message_id=message_id)
        return True

    etape = conv["etape"]
    # Convertir en int si stocké comme texte numérique par Supabase
    try:
        etape = int(etape)
    except (ValueError, TypeError):
        pass
    # Ignorer si la session est une session non-formulaire (STAT, close_reason, etc.)
    if not isinstance(etape, int):
        return False

    data = conv["data"] if isinstance(conv["data"], dict) else {}

    # ANNULATION / RESET
    if msg_upper in ["ANNULER", "CANCEL", "RESET"]:
        delete_conversation(expediteur)
        envoyer_whatsapp(expediteur,
            "❌ Signalement annulé.\n\nEnvoyez *#* pour recommencer.", message_id)
        return True

    # ---- NAVIGATION "!" = ÉTAPE PRÉCÉDENTE ----
    if msg_upper == "!":
        etape_precedente = ETAPES_PRECEDENTES.get(etape)
        if etape_precedente:
            # Si on revient à l'étape 1 et que le nom vient du profil, rester en étape 2
            user_info = get_role_utilisateur_info(expediteur)
            if etape_precedente == 1 and user_info and user_info.get("nom_prenom"):
                etape_precedente = 2
            set_conversation(expediteur, etape_precedente, data)
            envoyer_whatsapp(expediteur,
                f"↩️ Retour à l'étape précédente.", message_id)
            envoyer_formulaire_etape(expediteur, etape_precedente, data, message_id)
        else:
            # Etape 1 : annuler = fin
            delete_conversation(expediteur)
            envoyer_whatsapp(expediteur,
                "❌ Signalement annulé.\n\nEnvoyez *#* pour recommencer.", message_id)
        return True

    # ---- ETAPE 1 : NOM & PRENOM ----
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

    # ---- ETAPE 2 : TYPE PROJET ----
    elif etape == 2:
        type_proj = None
        for i, tp in enumerate(TYPES_PROJET, 1):
            if str(i) == msg_upper or tp == msg_upper:
                type_proj = tp
                break
        if not type_proj:
            envoyer_whatsapp(expediteur,
                "⚠️ TYPE NON RECONNU.\n\nChoisissez parmi:\nRAN, RURAL, FIBRE, CORE, IPRAN, MWV, MMONEY, HOME, AUTRES\n\nou répondez par le numéro (1 à 9).", message_id)
            return True
        data["type_projet"] = type_proj
        set_conversation(expediteur, 3, data)
        envoyer_formulaire_etape(expediteur, 3, message_id=message_id)
        return True

    # ---- ETAPE 3 : NOM PROJET ----
    elif etape == 3:
        data["nom_projet"] = message.strip()
        set_conversation(expediteur, 4, data)
        envoyer_formulaire_etape(expediteur, 4, message_id=message_id)
        return True

    # ---- ETAPE 4 : SITE ----
    elif etape == 4:
        data["site"] = message.strip()
        set_conversation(expediteur, 5, data)
        envoyer_formulaire_etape(expediteur, 5, message_id=message_id)
        return True

    # ---- ETAPE 5 : DESCRIPTION ----
    elif etape == 5:
        if len(message.strip()) < 10:
            envoyer_whatsapp(expediteur,
                "⚠️ DESCRIPTION TROP COURTE.\n\nMerci de décrire le risque plus en détail.", message_id)
            return True
        data["description"] = message.strip()
        set_conversation(expediteur, 6, data)
        envoyer_formulaire_etape(expediteur, 6, data, message_id=message_id)
        return True

    # ---- ETAPE 6 : CONFIRMATION ----
    elif etape == 6:
        if msg_upper in ["OK", "OUI", "VALIDER"]:
            return traiter_risque_confirme(expediteur, data, message_id)
        elif msg_upper in ["ANNULER", "NON", "RESET"]:
            delete_conversation(expediteur)
            envoyer_whatsapp(expediteur, "❌ Annulé. Envoyez *#* pour recommencer.", message_id)
            return True
        else:
            envoyer_whatsapp(expediteur,
                "Répondez *OK* pour valider, *!* pour modifier la description, ou *ANNULER* pour recommencer.", message_id)
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
            "❌ ERREUR DE SAUVEGARDE.\n\nVeuillez réessayer avec *#*.", message_id)
        return True

    risque_complet = recuperer_risque_par_id(risque_id)
    score_global = risque_complet.get("score_global", 15) if risque_complet else 15
    priorite = risque_complet.get("priorite", "ELEVE") if risque_complet else "ELEVE"

    emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}.get(priorite, "⚪")

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
        f"Votre responsable a été alerté.\n"
        f"🌐 Dashboard: {LIEN_DASHBOARD}\n\n"
        f"Pour fermer ce risque:\n"
        f"*CLOSE {risque_id}*"
    )
    envoyer_whatsapp(expediteur, feedback, message_id)
    delete_conversation(expediteur)

    # Escalade dynamique selon priorité — chercher par position
    destinataires_positions = ESCALADE_PRIORITE.get(priorite, [])
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
        f"Réagissez:\n"
        f"👍 = Pris en charge\n"
        f"⬆️ = Escalader\n\n"
        f"Pour fermer: *CLOSE {risque_id}*"
    )

    deja_notifies = set()
    for position in destinataires_positions:
        contact = get_astreinte(position)
        if contact and contact.get("telephone") and contact["telephone"] not in deja_notifies:
            envoyer_whatsapp(contact["telephone"], alerte, message_id)
            deja_notifies.add(contact["telephone"])

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
        set_conversation(expediteur, "close_reason", {"risque_id": risque_id, "action": "CLOSE"})
        envoyer_whatsapp(expediteur,
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔒 FERMETURE *{risque_id}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📍 SITE : {risque.get('site', '?').upper()}\n"
            f"🏗️ PROJET : {risque.get('nom_projet', '?').upper()}\n\n"
            f"Décrivez l'ACTION MENEE pour résoudre ce risque:\n\n"
            "_(Tapez *!* pour annuler)_", message_id)
        return True

    # --- UPDATE STATUT ---
    match_update = re.match(r'^UPDATE\s+([A-Z]{2,5}\d{8})\s+(OPENED|ASSIGNED|IN_PROGRESS|RESOLVED|CLOSED)\s+(.+)$', msg_upper)
    if match_update:
        risque_id = match_update.group(1)
        nouveau_statut = match_update.group(2)
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
    match_assign = re.match(r'^ASSIGN\s+([A-Z]{2,5}\d{8})\s+(\+?\d{10,15})$', msg_upper)
    if match_assign:
        risque_id = match_assign.group(1)
        numero_assignee = match_assign.group(2)
        # Normaliser : supprimer le + si présent
        numero_assignee = numero_assignee.lstrip("+")

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

        envoyer_whatsapp(numero_assignee,
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 RISQUE ASSIGNE\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 ID : *{risque_id}*\n"
            f"📁 PROJET : {risque.get('type_projet', '?')}\n"
            f"📍 SITE : {risque.get('site', '?').upper()}\n"
            f"⚠️ PRIORITE : {risque.get('priorite', '?')}\n\n"
            f"Vous êtes responsable de ce risque.\n"
            f"Pour mettre à jour: *UPDATE {risque_id} IN_PROGRESS votre action*\n"
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
            f"📝 ACTION : {risque.get('reponse_apportee', 'Aucune action renseignée')}", message_id)
        return True

    # --- MES-RISQUES ---
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

# ============================================================
# COMMANDE STAT
# ============================================================

def traiter_stat(expediteur, message, message_id=None):
    msg_upper = message.strip().upper()

    match_stat_global = re.match(r'^STAT$', msg_upper)
    match_stat_moi = re.match(r'^STAT\s+MOI$', msg_upper)
    match_stat_statut = re.match(r'^STAT\s+(OPENED|ASSIGNED|IN_PROGRESS|RESOLVED|CLOSED)$', msg_upper)
    match_stat_priorite = re.match(r'^STAT\s+(CRITIQUE|ELEVE|MOYEN|FAIBLE)$', msg_upper)
    match_stat_bloquants = re.match(r'^STAT\s+BLOQUANTS$', msg_upper)
    match_stat_semaine = re.match(r'^STAT\s+SEMAINE$', msg_upper)
    match_stat_7j = re.match(r'^STAT\s+7J$', msg_upper)
    match_stat_detail = re.match(r'^STAT\s+DETAIL$', msg_upper)
    match_stat_projet = re.match(r'^STAT\s+PROJET\s+([A-Z]+)$', msg_upper)
    match_stat_site = re.match(r'^STAT\s+SITE\s+(.+)$', msg_upper)
    match_stat_mois = re.match(r'^STAT\s+(\d{2}|JANVIER|FEVRIER|MARS|AVRIL|MAI|JUIN|JUILLET|AOUT|SEPTEMBRE|OCTOBRE|NOVEMBRE|DECEMBRE)$', msg_upper)

    if not any([match_stat_global, match_stat_moi, match_stat_statut, match_stat_priorite,
                match_stat_bloquants, match_stat_semaine, match_stat_7j, match_stat_detail,
                match_stat_projet, match_stat_site, match_stat_mois]):
        return False

    user_info = get_role_utilisateur_info(expediteur)
    role = user_info.get("role", "UTILISATEUR") if user_info else "UTILISATEUR"
    nom_user = user_info.get("nom_prenom", "Utilisateur") if user_info else "Utilisateur"

    requete_type = "global"
    filtre = None

    if match_stat_moi: requete_type = "moi"
    elif match_stat_statut: requete_type = "statut"; filtre = match_stat_statut.group(1)
    elif match_stat_priorite: requete_type = "priorite"; filtre = match_stat_priorite.group(1)
    elif match_stat_bloquants: requete_type = "bloquants"
    elif match_stat_semaine: requete_type = "semaine"
    elif match_stat_7j: requete_type = "7j"
    elif match_stat_detail: requete_type = "detail"
    elif match_stat_projet: requete_type = "projet"; filtre = match_stat_projet.group(1)
    elif match_stat_site: requete_type = "site"; filtre = match_stat_site.group(1)
    elif match_stat_mois: requete_type = "mois"; filtre = match_stat_mois.group(1)

    return executer_stat(expediteur, nom_user, role, requete_type, filtre, message_id)

def executer_stat(expediteur, nom_user, role, requete_type, filtre, message_id):
    supabase = get_supabase()
    if not supabase:
        envoyer_whatsapp(expediteur, "❌ Base de données non disponible.", message_id)
        return True

    try:
        query = supabase.table("risques").select("*")
        if role == "UTILISATEUR":
            query = query.or_(f"source_stakeholder_contact.eq.{expediteur},owner_contact.eq.{expediteur}")

        if requete_type == "moi":
            query = query.or_(f"source_stakeholder_contact.eq.{expediteur},owner_contact.eq.{expediteur}")
        elif requete_type == "statut" and filtre:
            query = query.eq("statut", filtre)
        elif requete_type == "priorite" and filtre:
            query = query.eq("priorite", filtre)
        elif requete_type == "bloquants":
            query = query.eq("bloque_projet", True)
        elif requete_type == "semaine":
            debut_semaine = (datetime.now() - timedelta(days=datetime.now().weekday())).isoformat()
            query = query.gte("date_identification", debut_semaine)
        elif requete_type == "7j":
            debut_7j = (datetime.now() - timedelta(days=7)).isoformat()
            query = query.gte("date_identification", debut_7j)
        elif requete_type == "mois" and filtre:
            mois_map = {
                'JANVIER':'01','FEVRIER':'02','MARS':'03','AVRIL':'04',
                'MAI':'05','JUIN':'06','JUILLET':'07','AOUT':'08',
                'SEPTEMBRE':'09','OCTOBRE':'10','NOVEMBRE':'11','DECEMBRE':'12'
            }
            mois_num = mois_map.get(filtre, filtre)
            aa = str(datetime.now().year)[-2:]
            query = query.like("risque_id", f"___{aa}{mois_num}%")
        elif requete_type == "projet" and filtre:
            query = query.eq("type_projet", filtre)
        elif requete_type == "site" and filtre:
            query = query.ilike("site", f"%{filtre}%")

        if requete_type not in ["statut"] or filtre != "CLOSED":
            query = query.or_("statut.neq.CLOSED,date_resolution.gte." + (datetime.now() - timedelta(days=30)).isoformat())

        result = query.order("date_identification", desc=True).limit(50).execute()
        risques = result.data if result.data else []

        if not risques:
            envoyer_whatsapp(expediteur,
                "━━━━━━━━━━━━━━━━━━━━━━\n📊 STATISTIQUES\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "ℹ️ Aucun risque trouvé pour cette requête.", message_id)
            return True

        total = len(risques)
        par_priorite = {}
        par_statut = {}
        bloquants = 0
        total_score = 0

        for r in risques:
            p = r.get("priorite", "MOYEN")
            par_priorite[p] = par_priorite.get(p, 0) + 1
            s = r.get("statut", "OPENED")
            par_statut[s] = par_statut.get(s, 0) + 1
            if r.get("bloque_projet"):
                bloquants += 1
            total_score += r.get("score_global", 0) or 0

        moyenne_score = total_score / total if total else 0

        emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}
        emoji_statut = {"OPENED": "🔴", "ASSIGNED": "🟠", "IN_PROGRESS": "🔵", "RESOLVED": "🟢", "CLOSED": "⚫"}

        vue_info = f" ({role})" if role in ["SUPERADMIN", "ADMIN", "CHEF_DE_PROJET", "PMO"] else " (vue perso)"

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 STAT{vue_info}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 {nom_user}\n"
            f"📈 {total} risque(s) | 💥 {bloquants} bloquant(s)\n"
            f"📊 Score moyen: {moyenne_score:.1f}/25\n\n"
        )

        msg += "PRIORITÉS: "
        prio_parts = []
        for prio in ["CRITIQUE", "ELEVE", "MOYEN", "FAIBLE"]:
            if prio in par_priorite:
                prio_parts.append(f"{emoji_priorite[prio]} {par_priorite[prio]}")
        msg += " | ".join(prio_parts) + "\n\n"

        msg += "STATUTS: "
        stat_parts = []
        for stat in ["OPENED", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED"]:
            if stat in par_statut:
                stat_parts.append(f"{emoji_statut[stat]} {par_statut[stat]}")
        msg += " | ".join(stat_parts) + "\n\n"

        msg += "TOP 3:\n"
        top3 = sorted(risques, key=lambda x: (x.get("score_global", 0), x.get("date_identification", "")), reverse=True)[:3]
        for i, r in enumerate(top3, 1):
            ep = emoji_priorite.get(r.get('priorite'), "⚪")
            es = emoji_statut.get(r.get('statut'), "⚪")
            nom_signalant = r.get('source_stakeholder_nom_prenom', '') or r.get('source_stakeholder_contact', '?')
            msg += (
                f"{i}. {ep}{es} *{r['risque_id']}* | {r.get('type_projet','?')}\n"
                f"   📍 {str(r.get('site','?')).upper()} | 👤 {nom_signalant}\n"
            )

        msg += (
            f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Répondez *1* pour les 10 suivants\n"
            f"Répondez *STAT DETAIL* pour tout voir\n"
            f"🌐 Dashboard: {LIEN_DASHBOARD}"
        )

        envoyer_whatsapp(expediteur, msg, message_id)

        if total > 3:
            set_conversation(expediteur, "stat_pagination", {
                "requete_type": requete_type,
                "filtre": filtre,
                "role": role,
                "offset": 3,
                "total": total
            })

        return True

    except Exception as e:
        print(f"==> Erreur STAT: {e}")
        envoyer_whatsapp(expediteur, "❌ Erreur lors de la récupération des statistiques.", message_id)
        return True

def traiter_stat_pagination(expediteur, message, message_id):
    msg_upper = message.strip().upper()
    if msg_upper != "1":
        return False

    conv = get_conversation(expediteur)
    if not conv or conv.get("etape") != "stat_pagination":
        return False

    data = conv.get("data", {})
    requete_type = data.get("requete_type", "global")
    filtre = data.get("filtre")
    role = data.get("role", "UTILISATEUR")
    offset = data.get("offset", 0)
    total = data.get("total", 0)

    supabase = get_supabase()
    if not supabase:
        return False

    try:
        query = supabase.table("risques").select("*")
        if role == "UTILISATEUR":
            query = query.or_(f"source_stakeholder_contact.eq.{expediteur},owner_contact.eq.{expediteur}")
        if requete_type == "moi":
            query = query.or_(f"source_stakeholder_contact.eq.{expediteur},owner_contact.eq.{expediteur}")
        elif requete_type == "statut" and filtre:
            query = query.eq("statut", filtre)
        elif requete_type == "priorite" and filtre:
            query = query.eq("priorite", filtre)
        elif requete_type == "bloquants":
            query = query.eq("bloque_projet", True)
        elif requete_type == "semaine":
            debut_semaine = (datetime.now() - timedelta(days=datetime.now().weekday())).isoformat()
            query = query.gte("date_identification", debut_semaine)
        elif requete_type == "7j":
            debut_7j = (datetime.now() - timedelta(days=7)).isoformat()
            query = query.gte("date_identification", debut_7j)
        elif requete_type == "mois" and filtre:
            mois_map = {
                'JANVIER':'01','FEVRIER':'02','MARS':'03','AVRIL':'04',
                'MAI':'05','JUIN':'06','JUILLET':'07','AOUT':'08',
                'SEPTEMBRE':'09','OCTOBRE':'10','NOVEMBRE':'11','DECEMBRE':'12'
            }
            mois_num = mois_map.get(filtre, filtre)
            aa = str(datetime.now().year)[-2:]
            query = query.like("risque_id", f"___{aa}{mois_num}%")
        elif requete_type == "projet" and filtre:
            query = query.eq("type_projet", filtre)
        elif requete_type == "site" and filtre:
            query = query.ilike("site", f"%{filtre}%")
        if requete_type not in ["statut"] or filtre != "CLOSED":
            query = query.or_("statut.neq.CLOSED,date_resolution.gte." + (datetime.now() - timedelta(days=30)).isoformat())
        result = query.order("date_identification", desc=True).range(offset, offset + 9).execute()
        risques = result.data if result.data else []

        if not risques:
            envoyer_whatsapp(expediteur, "ℹ️ Plus de risques à afficher.", message_id)
            delete_conversation(expediteur)
            return True

        emoji_priorite = {"CRITIQUE": "🔴", "ELEVE": "🟠", "MOYEN": "🟡", "FAIBLE": "🟢"}
        emoji_statut = {"OPENED": "🔴", "ASSIGNED": "🟠", "IN_PROGRESS": "🔵", "RESOLVED": "🟢", "CLOSED": "⚫"}
        msg = f"📋 SUIVI ({offset+1}-{offset+len(risques)}/{total})\n\n"

        for i, r in enumerate(risques, offset + 1):
            ep = emoji_priorite.get(r.get('priorite'), "⚪")
            es = emoji_statut.get(r.get('statut'), "⚪")
            nom_signalant = r.get('source_stakeholder_nom_prenom', '') or r.get('source_stakeholder_contact', '?')
            msg += (
                f"{i}. {ep}{es} *{r['risque_id']}*\n"
                f"   📍 {str(r.get('site','?')).upper()} | {r.get('type_projet','?')}\n"
                f"   👤 {nom_signalant} | 📊 {r.get('score_global','?')}/25\n\n"
            )

        nouveau_offset = offset + len(risques)
        if nouveau_offset < total:
            msg += "Répondez *1* pour continuer\n"
            set_conversation(expediteur, "stat_pagination", {**data, "offset": nouveau_offset})
        else:
            msg += "✅ Fin de la liste\n"
            delete_conversation(expediteur)

        msg += f"🌐 Dashboard: {LIEN_DASHBOARD}"
        envoyer_whatsapp(expediteur, msg, message_id)
        return True

    except Exception as e:
        print(f"==> Erreur pagination STAT: {e}")
        return True

# Gestion CLOSE avec raison (avec support "!" pour annuler)
def traiter_close_avec_raison(expediteur, message, message_id):
    conv = get_conversation(expediteur)
    if not conv or conv.get("etape") != "close_reason":
        return False

    msg_upper = message.strip().upper()
    data = conv.get("data", {})
    risque_id = data.get("risque_id")

    # "!" annule la fermeture
    if msg_upper == "!":
        delete_conversation(expediteur)
        envoyer_whatsapp(expediteur,
            f"↩️ Fermeture annulée pour *{risque_id}*.\n\nLe risque reste ouvert.", message_id)
        return True

    raison = message.strip()
    if len(raison) < 5:
        envoyer_whatsapp(expediteur,
            "⚠️ Veuillez décrire l'ACTION MENEE plus en détail.\n_(Tapez *!* pour annuler)_", message_id)
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
        'JANVIER':1,'JAN':1,'FEVRIER':2,'FEV':2,'MARS':3,'MAR':3,
        'AVRIL':4,'AVR':4,'MAI':5,'JUIN':6,'JUI':6,'JUILLET':7,
        'AOUT':8,'AOU':8,'SEPTEMBRE':9,'SEP':9,'OCTOBRE':10,'OCT':10,
        'NOVEMBRE':11,'NOV':11,'DECEMBRE':12,'DEC':12
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
Structure: 1.RESUME EXECUTIF 2.CHIFFRES CLES 3.TOP 3 POINTS 4.TENDANCES 5.SCORE SANTE 6.DECISIONS URGENTES"""

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
            rapport = f"Rapport semaine {datetime.now().isocalendar()[1]}\nTotal: {total} | Score: {moy_score:.1f}/25 | Bloquants: {bloquants}"

        supabase.table("rapports_hebdo").insert({
            "semaine": datetime.now().isocalendar()[1],
            "annee": datetime.now().year,
            "contenu": rapport,
            "kpi_json": {
                "total": total, "par_priorite": par_priorite,
                "par_type_projet": par_type_projet,
                "score_moyen": moy_score, "bloquants": bloquants
            },
            "date_envoi": datetime.now().isoformat()
        }).execute()

        admins = get_admins_actifs()
        for admin in admins:
            envoyer_whatsapp(admin["telephone"],
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
                "CRITIQUE": (15, "CHEF_DE_PROJET", "PMO"),
                "ELEVE":    (30, "CHEF_DE_PROJET", "DIRECTEUR_PROJET"),
                "MOYEN":    (60, "CHEF_DE_PROJET", "DIRECTEUR_PROJET")
            }
            if priorite in regles:
                delai, _, niveau2 = regles[priorite]
                if minutes > delai and alerte.get("niveau_escalade", 0) < 1:
                    contact = get_astreinte(niveau2)
                    if contact and contact.get("telephone"):
                        envoyer_whatsapp(contact["telephone"],
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
    return "DigitalRiskPlatform v6.0 — Navigation !, Inscription auto, Double Admin", 200

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

        # Réactions emoji
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
            print(f"==> Message dupliqué ignoré: {msg_id}")
            return jsonify({"status": "ok"})
        marquer_message_traite(msg_id)

        message = msg_data["text"]["body"]
        expediteur = msg_data["from"]
        print(f"==> Message: {message} | De: {expediteur}")

        # ---- VERIFICATION INSCRIPTION ----
        # Le SuperAdmin fixe est toujours autorisé
        if expediteur != SUPERADMIN_PHONE:
            # Vérifier D'ABORD si une session d'inscription est en cours
            # (l'utilisateur n'est pas encore en base mais a déjà commencé l'inscription)
            if traiter_inscription(expediteur, message, msg_id):
                return jsonify({"status": "inscription"})

            user = get_utilisateur(expediteur)
            if not user:
                # Nouvel utilisateur sans session : démarrer l'inscription
                demarrer_inscription(expediteur, msg_id)
                return jsonify({"status": "inscription_demarree"})

            # Si compte inactif (désactivé par admin), bloquer
            if not user.get("actif", False):
                envoyer_whatsapp(expediteur,
                    "⚠️ Votre compte est désactivé.\n"
                    "Contactez votre administrateur.", msg_id)
                return jsonify({"status": "compte_desactive"})
        else:
            # SuperAdmin : vérifier si une session d'inscription est en cours (ne devrait pas, mais sécurité)
            if traiter_inscription(expediteur, message, msg_id):
                return jsonify({"status": "inscription"})
            # SuperAdmin pas encore en base → créer automatiquement
            user_sa = get_utilisateur(expediteur)
            if not user_sa:
                creer_utilisateur(
                    telephone=expediteur,
                    nom_prenom="Super Admin",
                    position="DIRECTEUR_PROJET",
                    role="SUPERADMIN",
                    actif=True,
                    valide=True
                )
                envoyer_whatsapp(expediteur,
                    "SuperAdmin configure. Tapez *AIDE* pour voir toutes les commandes.",
                    msg_id)
                return jsonify({"status": "superadmin_created"})

        # ---- COMMANDES ADMIN ----
        if traiter_commandes_admin(expediteur, message, msg_id):
            return jsonify({"status": "commande_admin"})

        # ---- SESSION CLOSE EN ATTENTE ----
        if traiter_close_avec_raison(expediteur, message, msg_id):
            return jsonify({"status": "close_raison"})

        # ---- COMMANDES STAT ----
        if traiter_stat(expediteur, message, msg_id):
            return jsonify({"status": "stat"})

        # ---- PAGINATION STAT ----
        if traiter_stat_pagination(expediteur, message, msg_id):
            return jsonify({"status": "stat_pagination"})

        # ---- COMMANDES MANAGER ----
        if traiter_commande_manager(expediteur, message, msg_id):
            return jsonify({"status": "commande_manager"})

        # ---- RECHERCHE PERIODE ----
        if traiter_recherche_periode(expediteur, message, msg_id):
            return jsonify({"status": "recherche_periode"})

        # ---- COMMANDES GENERALES ----
        cmd = message.strip().upper()

        if cmd in ["RAPPORT", "DASHBOARD"]:
            envoyer_whatsapp(expediteur, f"🌐 Dashboard: {LIEN_DASHBOARD}", msg_id)
            return jsonify({"status": "ok"})

        if cmd in ["AIDE", "HELP", "MENU"]:
            # Contenu AIDE adapté selon rôle
            user_info = get_role_utilisateur_info(expediteur)
            role = user_info.get("role", "UTILISATEUR") if user_info else "UTILISATEUR"

            aide_admin = ""
            if role in ["SUPERADMIN", "ADMIN"]:
                aide_admin = (
                    "\n👑 ADMINISTRATION:\n"
                    "UTILISATEURS — Liste tous les membres\n"
                    "VALIDER +225X 3 — Valider inscription (1=SA, 2=Admin, 3=User)\n"
                    "REFUSER +225X — Refuser une inscription\n"
                    "ROLE +225X 2 — Modifier le rôle\n"
                    "POSITION +225X 4 — Modifier la position\n"
                    "DESACTIVER +225X — Désactiver un compte\n"
                )

            envoyer_whatsapp(expediteur,
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📱 DIGITALRISKPLATFORM v6\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "📋 SIGNALER UN RISQUE:\n"
                "Envoyez *#* pour commencer\n"
                "_(Tapez *!* à tout moment pour revenir en arrière)_\n\n"
                "📊 TABLEAU DE BORD (STAT):\n"
                "STAT — Vue globale\n"
                "STAT MOI — Mes risques\n"
                "STAT OPENED/ASSIGNED/... — Par statut\n"
                "STAT CRITIQUE/ELEVE/... — Par priorité\n"
                "STAT BLOQUANTS — Risques bloquants\n"
                "STAT SEMAINE / STAT 7J — Récents\n"
                "STAT MAI / STAT PROJET RAN / STAT SITE X\n\n"
                "📊 RECHERCHE:\n"
                "LISTE MAI [2026] — Risques du mois\n"
                "STATS MAI [2026] — Statistiques\n\n"
                "👤 MES RISQUES:\n"
                "MES-RISQUES — Tous les risques ouverts\n"
                "RISQUES-SITE COCODY — Par site\n\n"
                "🔧 GESTION:\n"
                "STATUS XXXAAMM0000 — Voir statut\n"
                "CLOSE XXXAAMM0000 — Fermer\n"
                "UPDATE XXXAAMM0000 IN_PROGRESS action\n"
                "ASSIGN XXXAAMM0000 +225X\n"
                f"{aide_admin}\n"
                "🌐 RAPPORT — Dashboard", msg_id)
            return jsonify({"status": "ok"})

        # ---- FORMULAIRE INTERACTIF ----
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
        return "Supabase non configuré", 500
    try:
        result = supabase.table("risques").select("count", count="exact").execute()
        count = result.count if hasattr(result, 'count') else "OK"
        return f"Connexion OK — Risques: {count}", 200
    except Exception as e:
        return f"Erreur: {str(e)}", 500

@app.route("/cron/escalades")
def cron_escalades():
    verifier_escalades_en_attente()
    return "Escalades vérifiées", 200

@app.route("/cron/rapport-hebdo")
def cron_rapport_hebdo():
    generer_rapport_hebdo()
    return "Rapport généré", 200



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
    print("==> Scheduler démarré : escalades (5min) + rapport (lundi 8h) + validations (2min)")
    return scheduler

if __name__ == "__main__":
    demarrer_scheduler()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
