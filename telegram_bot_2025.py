import logging
import os  # AJOUTÉ
from dotenv import load_dotenv  # AJOUTÉ
load_dotenv()  # AJOUTÉ
from typing import Dict, Any, Optional, List

import aiosqlite
import aiohttp
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

(
    LANGUAGE,         # choix FR / EN
    AGE,
    GENRE,
    TARGET,
    CHOIX_LOCALISATION,
    CITY,
    CHOIX_VILLE_PRECISE,  # Nouvel état pour choisir parmi plusieurs villes
    NAME,
    BIO,
    PHOTO,
    MENU,
    RECHERCHE_MATCH,
    EDITION_BIO,
    MYPROFILE_MENU,
    EDITION_PHOTO,
) = range(15)

# Gestion de la base de données selon l'environnement
if os.environ.get("RENDER"):
    # Sur Render, utiliser un chemin persistant
    FICHIER_BD = "/tmp/bot_rencontres_2025.db"
else:
    # En local
    FICHIER_BD = "bot_rencontres_2025.db"
ADMIN_ID = 2063019308
SUPPORT_USERNAME = "DashawnAfrica_help"


# --- Helpers langue ---------------------------------------------------------


async def get_user_lang(source, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Retourne la langue de l'utilisateur ('fr' ou 'en').
    Priorité : context.user_data -> base de données -> 'fr'

    source peut être un Update, un Message, ou tout objet ayant .effective_user ou .from_user.
    """
    # 1. D'abord ce qu'on a en mémoire pour cet utilisateur
    lang = context.user_data.get("langue")
    if lang in ("fr", "en"):
        return lang

    # 2. Retrouver l'id utilisateur à partir de source
    user_id = None
    if hasattr(source, "effective_user") and getattr(source, "effective_user", None):
        user_id = source.effective_user.id
    else:
        user = getattr(source, "from_user", None)
        if user:
            user_id = user.id

    if user_id is None:
        return "fr"

    # 3. Regarder dans la base
    db = context.bot_data.get("bd")
    if db is None:
        return "fr"

    async with db.execute(
        "SELECT langue FROM utilisateurs WHERE id_utilisateur = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()

    if row and row[0] in ("fr", "en"):
        context.user_data["langue"] = row[0]
        return row[0]

    return "fr"


def get_lang_from_context(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("langue", "fr")


# --- Base de données --------------------------------------------------------


async def initialiser_bd(app: Application):
    db = await aiosqlite.connect(FICHIER_BD)

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS utilisateurs (
            id_utilisateur INTEGER PRIMARY KEY,
            username TEXT,
            nom TEXT,
            age INTEGER,
            genre TEXT,
            cible_genre TEXT,
            pays TEXT,
            ville TEXT,
            bio TEXT,
            id_photo TEXT,
            latitude REAL,
            longitude REAL,
            langue TEXT,
            date_inscription DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    async with db.execute("PRAGMA table_info(utilisateurs)") as cursor:
        colonnes = [row[1] for row in await cursor.fetchall()]

    colonnes_a_ajouter = [
        ("username", "TEXT"),
        ("nom", "TEXT"),
        ("cible_genre", "TEXT"),
        ("pays", "TEXT"),
        ("ville", "TEXT"),
        ("bio", "TEXT"),
        ("id_photo", "TEXT"),
        ("latitude", "REAL"),
        ("longitude", "REAL"),
        ("langue", "TEXT"),
    ]
    for nom_col, type_col in colonnes_a_ajouter:
        if nom_col not in colonnes:
            await db.execute(f"ALTER TABLE utilisateurs ADD COLUMN {nom_col} {type_col}")

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS swipes (
            id_swipeur INTEGER NOT NULL,
            id_swipe INTEGER NOT NULL,
            action TEXT NOT NULL,
            date_swipe DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id_swipeur, id_swipe)
        )
        """
    )

    await db.commit()
    app.bot_data["bd"] = db
    logger.info("Base de données connectée et tables prêtes.")


async def fermer_bd(app: Application):
    await app.bot_data["bd"].close()
    logger.info("Connexion à la base de données fermée.")


async def utilisateur_existe(id_utilisateur: int, db: aiosqlite.Connection) -> bool:
    async with db.execute(
        "SELECT 1 FROM utilisateurs WHERE id_utilisateur = ?",
        (id_utilisateur,),
    ) as cursor:
        return await cursor.fetchone() is not None


# --- Reverse geocoding : coordonnées -> ville & pays ------------------------


async def trouver_ville_et_pays_par_coordonnees(
    latitude: float, longitude: float
) -> tuple[Optional[str], Optional[str]]:
    """
    Retourne (ville, pays) à partir des coordonnées GPS.
    """
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": str(latitude),
        "lon": str(longitude),
        "format": "jsonv2",
        "accept-language": "fr",
    }
    headers = {
        "User-Agent": "DashawnAfricaDatingBot/1.0",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, headers=headers, timeout=10
            ) as resp:
                if resp.status != 200:
                    logger.warning("Reverse geocoding HTTP %s", resp.status)
                    return None, None
                data = await resp.json()
                address = data.get("address", {})

                ville = (
                    address.get("city")
                    or address.get("town")
                    or address.get("village")
                    or address.get("municipality")
                    or address.get("state")
                )
                pays = address.get("country")

                return ville, pays
    except Exception as e:
        logger.warning("Erreur reverse geocoding: %s", e)
        return None, None


# --- Géocodage : nom de ville -> liste de résultats ------------------------


async def rechercher_villes_par_nom(nom_ville: str, lang: str = "fr") -> List[Dict[str, Any]]:
    """
    Recherche des villes par nom via l'API Nominatim (search).
    Retourne une liste de dictionnaires avec :
    - display_name: nom complet affichable
    - ville: nom de la ville
    - pays: nom du pays
    - lat: latitude
    - lon: longitude
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": nom_ville,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 5,  # Limiter à 5 résultats
        "accept-language": lang,
    }
    headers = {
        "User-Agent": "DashawnAfricaDatingBot/1.0",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, headers=headers, timeout=10
            ) as resp:
                if resp.status != 200:
                    logger.warning("Geocoding HTTP %s", resp.status)
                    return []
                data = await resp.json()
                
                resultats = []
                for item in data:
                    # Extraire les informations pertinentes
                    address = item.get("address", {})
                    ville = (
                        address.get("city")
                        or address.get("town")
                        or address.get("village")
                        or address.get("municipality")
                        or address.get("state")
                    )
                    pays = address.get("country")
                    
                    if ville and pays:
                        resultats.append({
                            "display_name": item.get("display_name", ""),
                            "ville": ville,
                            "pays": pays,
                            "lat": float(item.get("lat", 0)),
                            "lon": float(item.get("lon", 0))
                        })
                
                return resultats
    except Exception as e:
        logger.warning("Erreur géocodage: %s", e)
        return []


# --- Claviers ----------------------------------------------------------------


def menu_principal_clavier(lang: str = "fr") -> ReplyKeyboardMarkup:
    if lang == "en":
        return ReplyKeyboardMarkup(
            [["Find a match 💘"], ["My profile 👤"]],
            resize_keyboard=True,
        )
    else:
        return ReplyKeyboardMarkup(
            [["Chercher une correspondance 💘"], ["Mon profil 👤"]],
            resize_keyboard=True,
        )


def clavier_recherche_match(lang: str = "fr") -> ReplyKeyboardMarkup:
    """Clavier utilisé pendant la recherche de correspondances."""
    if lang == "en":
        return ReplyKeyboardMarkup(
            [["❤️ Like", "❌ Skip"]],
            resize_keyboard=True,
        )
    else:
        return ReplyKeyboardMarkup(
            [["❤️ J'aime", "❌ Passer"]],
            resize_keyboard=True,
        )


def clavier_selection_ville(resultats: List[Dict], lang: str = "fr") -> ReplyKeyboardMarkup:
    """
    Crée un clavier pour la sélection de ville.
    Inclut les numéros des résultats et un bouton pour partager la localisation.
    """
    # Créer les boutons numérotés
    boutons_numeros = []
    for i in range(len(resultats)):
        boutons_numeros.append(str(i + 1))
    
    # Organiser en lignes de 3 boutons maximum
    lignes_numeros = []
    for i in range(0, len(boutons_numeros), 3):
        lignes_numeros.append(boutons_numeros[i:i+3])
    
    # Ajouter le bouton de localisation
    if lang == "en":
        bouton_localisation = ["📍 Share my location"]
    else:
        bouton_localisation = ["📍 Envoyer ma position"]
    
    # Construire le clavier
    lignes_clavier = lignes_numeros + [bouton_localisation]
    
    return ReplyKeyboardMarkup(
        lignes_clavier,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# --- Normalisation / traduction genre & cible pour l'affichage -------------


def normaliser_genre_pour_affichage(genre: Optional[str], lang: str) -> str:
    if not genre:
        return ""  # on gère le texte "Non renseigné" / "Not set" ailleurs

    g = genre.strip().lower()

    if lang == "fr":
        if g in ("homme", "man"):
            return "Homme"
        if g in ("femme", "woman"):
            return "Femme"
        if g in ("autre", "other"):
            return "Autre"
    else:  # lang == "en"
        if g in ("homme", "man"):
            return "Man"
        if g in ("femme", "woman"):
            return "Woman"
        if g in ("autre", "other"):
            return "Other"

    # si on ne reconnait pas, on renvoie tel quel
    return genre


def normaliser_cible_pour_affichage(cible: Optional[str], lang: str) -> str:
    if not cible:
        return ""

    c = cible.strip().lower()

    if lang == "fr":
        if c in ("women", "femmes"):
            return "Femmes"
        if c in ("men", "hommes"):
            return "Hommes"
        if c in ("doesn't matter", "peu importe"):
            return "Peu importe"
    else:  # lang == "en"
        if c in ("women", "femmes"):
            return "Women"
        if c in ("men", "hommes"):
            return "Men"
        if c in ("peu importe", "doesn't matter"):
            return "Doesn't matter"

    return cible


# --- Messages de partage / limite -------------------------------------------


async def envoyer_message_partage_plus_tard(message_obj, context):
    """Quand il n'y a plus de profils à proposer."""
    lang = await get_user_lang(message_obj, context)
    bot_username = context.bot.username
    lien_bot = f"https://t.me/{bot_username}"

    if lang == "en":
        texte = (
            "It looks like there are no more potential matches for now.\n"
            "Try again later! 😊\n\n"
            "You can also invite friends to get more profiles ❤️\n\n"
            f"Here is the bot link to share:\n{lien_bot}"
        )
    else:
        texte = (
            "Il semble qu'il n'y ait plus de correspondances potentielles pour le moment. "
            "Réessaie plus tard ! 😊\n\n"
            "Tu peux aussi inviter des amis pour avoir plus de profils ❤️\n\n"
            f"Voici le lien du bot à partager :\n{lien_bot}"
        )

    await message_obj.reply_text(
        texte,
        reply_markup=menu_principal_clavier(lang),
        disable_web_page_preview=True,
    )


async def envoyer_message_limite(message_obj, context):
    """Quand l'utilisateur a déjà réagi à 15 profils en 24h."""
    lang = await get_user_lang(message_obj, context)
    bot_username = context.bot.username
    lien_bot = f"https://t.me/{bot_username}"

    if lang == "en":
        texte = (
            "Too many ❤️ for today!\n\n"
            "You have reached the limit of 15 profiles in 24 hours.\n"
            "Invite friends to get even more matches ❤️\n\n"
            f"Here is the bot link to share:\n{lien_bot}"
        )
    else:
        texte = (
            "Trop de ❤️ pour aujourd'hui !\n\n"
            "Tu as atteint la limite de 15 profils en 24 heures.\n"
            "Invite des amis pour avoir encore plus de rencontres ❤️\n\n"
            f"Voici le lien du bot à partager :\n{lien_bot}"
        )

    await message_obj.reply_text(
        texte,
        reply_markup=menu_principal_clavier(lang),
        disable_web_page_preview=True,
    )


# --- Flux d'inscription -----------------------------------------------------


async def demarrage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    utilisateur = update.effective_user
    db = context.bot_data["bd"]

    if await utilisateur_existe(utilisateur.id, db):
        lang = await get_user_lang(update, context)
        if lang == "en":
            texte = (
                "Welcome back! 🎉\n\n"
                "Use the menu below to start finding matches or manage your profile."
            )
        else:
            texte = (
                "Bon retour ! 🎉\n\n"
                "Utilise le menu ci-dessous pour chercher des correspondances ou gérer ton profil."
            )
        await update.message.reply_text(
            texte,
            reply_markup=menu_principal_clavier(lang),
        )
        await renvoyer_like_en_attente(update, context)
        return MENU
    else:
        # Choix de langue
        clavier_lang = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🇫🇷 Français", callback_data="lang_fr"),
                    InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
                ]
            ]
        )
        await update.message.reply_text(
            "Choisis ta langue / Choose your language :", reply_markup=clavier_lang
        )
        return LANGUAGE


async def language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "lang_en":
        lang = "en"
    else:
        lang = "fr"

    context.user_data["langue"] = lang

    if lang == "en":
        texte = (
            f"Hello, {update.effective_user.first_name}! Welcome to the dating bot 💘\n\n"
            "Let's start with a few questions to create your profile.\n\n"
            "How old are you?"
        )
    else:
        texte = (
            f"Bonjour, {update.effective_user.first_name} ! Bienvenue dans le bot de rencontres 💘\n\n"
            "Commençons par quelques questions pour créer ton profil.\n\n"
            "Quel âge as-tu ?"
        )

    await query.message.edit_text(texte)
    return AGE


async def age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_lang_from_context(context)
    texte = (update.message.text or "").strip()
    if not texte.isdigit():
        if lang == "en":
            msg = "Please enter your age as a number, e.g. 24."
        else:
            msg = "Merci d'indiquer ton âge avec un nombre, par ex. 24."
        await update.message.reply_text(msg)
        return AGE

    age = int(texte)
    if age < 18:
        if lang == "en":
            msg = "❌ This service is only for adults (18+)."
        else:
            msg = "❌ Ce service est réservé aux adultes (18+)."
        await update.message.reply_text(msg)
        return ConversationHandler.END

    context.user_data["age"] = age

    # Choix de genre
    if lang == "en":
        clavier = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("I am a woman", callback_data="gender_female")],
                [InlineKeyboardButton("I am a man", callback_data="gender_male")],
                [InlineKeyboardButton("Other", callback_data="gender_other")],
            ]
        )
        texte = "Let's choose your gender:"
    else:
        clavier = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Je suis une femme", callback_data="gender_female")],
                [InlineKeyboardButton("Je suis un homme", callback_data="gender_male")],
                [InlineKeyboardButton("Autre", callback_data="gender_other")],
            ]
        )
        texte = "Choisissons ton genre :"

    await update.message.reply_text(texte, reply_markup=clavier)
    return GENRE


async def genre_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = get_lang_from_context(context)

    data = query.data
    if data == "gender_female":
        genre = "Femme" if lang == "fr" else "Woman"
    elif data == "gender_male":
        genre = "Homme" if lang == "fr" else "Man"
    else:
        genre = "Autre" if lang == "fr" else "Other"

    context.user_data["genre"] = genre

    # Qui t'intéresse ?
    if lang == "en":
        clavier = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Women", callback_data="target_girls")],
                [InlineKeyboardButton("Men", callback_data="target_boys")],
                [InlineKeyboardButton("Doesn't matter", callback_data="target_all")],
            ]
        )
        texte = "Who are you interested in?"
    else:
        clavier = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Femmes", callback_data="target_girls")],
                [InlineKeyboardButton("Hommes", callback_data="target_boys")],
                [InlineKeyboardButton("Peu importe", callback_data="target_all")],
            ]
        )
        texte = "Qui t'intéresse ?"

    await query.edit_message_text(texte, reply_markup=clavier)
    return TARGET


async def target_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = get_lang_from_context(context)

    data = query.data
    if data == "target_girls":
        cible = "Femmes" if lang == "fr" else "Women"
    elif data == "target_boys":
        cible = "Hommes" if lang == "fr" else "Men"
    else:
        cible = "Peu importe" if lang == "fr" else "Doesn't matter"

    context.user_data["cible_genre"] = cible

    if lang == "en":
        clavier = ReplyKeyboardMarkup(
            [
                [KeyboardButton("📍 Share my location", request_location=True)],
                [KeyboardButton("🏙️ Enter my city")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        texte = (
            "Great! Now tell me where you live.\n\n"
            "You can choose:\n"
            "• 📍 Share your current location\n"
            "• 🏙️ Enter the name of your city"
        )
    else:
        clavier = ReplyKeyboardMarkup(
            [
                [KeyboardButton("📍 Partager ma localisation", request_location=True)],
                [KeyboardButton("🏙️ Indiquer ma ville")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        texte = (
            "Super ! Maintenant, indique où tu habites.\n\n"
            "Tu peux choisir :\n"
            "• 📍 Partager ta localisation actuelle\n"
            "• 🏙️ Indiquer simplement le nom de ta ville"
        )

    await query.message.reply_text(texte, reply_markup=clavier)
    return CHOIX_LOCALISATION


async def choix_localisation_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    lang = get_lang_from_context(context)

    if message.location:
        loc = message.location
        context.user_data["latitude"] = loc.latitude
        context.user_data["longitude"] = loc.longitude

        ville, pays = await trouver_ville_et_pays_par_coordonnees(
            loc.latitude, loc.longitude
        )
        if ville:
            context.user_data["ville"] = ville
        if pays:
            context.user_data["pays"] = pays

        if lang == "en":
            texte = "Thanks! Now, how should I call you?"
        else:
            texte = "Merci ! Maintenant, comment veux-tu que je t'appelle ?"

        await message.reply_text(texte, reply_markup=ReplyKeyboardRemove())
        return NAME

    texte = (message.text or "").strip().lower()

    if "indiquer ma ville" in texte or "enter my city" in texte:
        if lang == "en":
            msg = "Okay, just type the name of your city (e.g. Cotonou, Paris, Accra...)."
        else:
            msg = "D'accord, écris simplement le nom de ta ville (par ex. Cotonou, Paris, Abidjan...)."
        await message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
        return CITY

    # L'utilisateur a directement tapé sa ville, sans partager la localisation
    # On passe directement au géocodage
    context.user_data["ville_input"] = message.text
    return await process_city_input(update, context)


async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handler pour l'état CITY - l'utilisateur a tapé un nom de ville."""
    ville_input = (update.message.text or "").strip()
    context.user_data["ville_input"] = ville_input
    return await process_city_input(update, context)


async def process_city_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Traite l'input de ville: recherche géocodage et propose les résultats."""
    ville_input = context.user_data.get("ville_input", "")
    lang = get_lang_from_context(context)
    
    if not ville_input:
        if lang == "en":
            msg = "Please enter a city name."
        else:
            msg = "Merci d'entrer un nom de ville."
        await update.message.reply_text(msg)
        return CITY
    
    # Recherche géocodage
    if lang == "en":
        await update.message.reply_text("Searching for cities... 🔍")
    else:
        await update.message.reply_text("Recherche de villes... 🔍")
    
    resultats = await rechercher_villes_par_nom(ville_input, lang)
    
    if not resultats:
        if lang == "en":
            msg = (
                f"I didn't find any city named '{ville_input}'.\n"
                "Please try again with a different name or share your location."
            )
        else:
            msg = (
                f"Je n'ai trouvé aucune ville nommée '{ville_input}'.\n"
                "Veuillez réessayer avec un nom différent ou partager votre localisation."
            )
        await update.message.reply_text(msg)
        return CITY
    
    if len(resultats) == 1:
        # Un seul résultat, on l'enregistre directement
        resultat = resultats[0]
        context.user_data["ville"] = resultat["ville"]
        context.user_data["pays"] = resultat["pays"]
        context.user_data["latitude"] = resultat["lat"]
        context.user_data["longitude"] = resultat["lon"]
        
        if lang == "en":
            msg = f"Found: {resultat['ville']}, {resultat['pays']}\n\nNow, how should I call you?"
        else:
            msg = f"Trouvé : {resultat['ville']}, {resultat['pays']}\n\nMaintenant, comment veux-tu que je t'appelle ?"
        
        await update.message.reply_text(msg)
        return NAME
    else:
        # Plusieurs résultats, on propose une liste
        context.user_data["city_candidates"] = resultats
        
        # Construire le message avec la liste numérotée
        if lang == "en":
            message_text = "Write the number of the city or specify the name:\n\n"
        else:
            message_text = "Écris le numéro de la ville ou précise le nom :\n\n"
        
        for i, resultat in enumerate(resultats, 1):
            # Formater l'affichage (ville, pays)
            display_name = f"{resultat['ville']}, {resultat['pays']}"
            message_text += f"{i}. {display_name}\n"
        
        # Clavier pour la sélection
        clavier = clavier_selection_ville(resultats, lang)
        
        await update.message.reply_text(message_text, reply_markup=clavier)
        return CHOIX_VILLE_PRECISE


async def choix_ville_precise_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handler pour l'état CHOIX_VILLE_PRECISE - choix parmi plusieurs villes."""
    message = update.message
    texte = (message.text or "").strip()
    lang = get_lang_from_context(context)
    
    # Vérifier si l'utilisateur envoie sa position
    if message.location:
        loc = message.location
        context.user_data["latitude"] = loc.latitude
        context.user_data["longitude"] = loc.longitude

        ville, pays = await trouver_ville_et_pays_par_coordonnees(
            loc.latitude, loc.longitude
        )
        if ville:
            context.user_data["ville"] = ville
        if pays:
            context.user_data["pays"] = pays

        if lang == "en":
            texte = "Thanks! Now, how should I call you?"
        else:
            texte = "Merci ! Maintenant, comment veux-tu que je t'appelle ?"

        await message.reply_text(texte, reply_markup=ReplyKeyboardRemove())
        return NAME
    
    # Vérifier si c'est un numéro valide
    if texte.isdigit():
        index = int(texte) - 1
        resultats = context.user_data.get("city_candidates", [])
        
        if 0 <= index < len(resultats):
            resultat = resultats[index]
            context.user_data["ville"] = resultat["ville"]
            context.user_data["pays"] = resultat["pays"]
            context.user_data["latitude"] = resultat["lat"]
            context.user_data["longitude"] = resultat["lon"]
            
            if lang == "en":
                msg = f"You selected: {resultat['ville']}, {resultat['pays']}\n\nNow, how should I call you?"
            else:
                msg = f"Tu as sélectionné : {resultat['ville']}, {resultat['pays']}\n\nMaintenant, comment veux-tu que je t'appelle ?"
            
            await message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
            return NAME
        else:
            if lang == "en":
                msg = f"Please choose a number between 1 and {len(resultats)}."
            else:
                msg = f"Veuillez choisir un nombre entre 1 et {len(resultats)}."
            await message.reply_text(msg)
            return CHOIX_VILLE_PRECISE
    else:
        # L'utilisateur a tapé un nouveau texte, relancer une recherche
        context.user_data["ville_input"] = texte
        return await process_city_input(update, context)


async def name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_lang_from_context(context)
    nom = (update.message.text or "").strip()
    context.user_data["nom"] = nom
    # On fige le nom choisi pendant cette inscription
    context.user_data["nom_fixe"] = nom

    clavier = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Passer / Skip", callback_data="bio_skip")]]
    )

    if lang == "en":
        texte = (
            "Tell a bit about yourself: who you are, who you want to meet, what you like to do.\n\n"
            "It will help me find better matches for you."
        )
    else:
        texte = (
            "Parle un peu de toi : qui tu es, qui tu veux rencontrer, ce que tu proposes de faire.\n\n"
            "Ça m'aidera à mieux te trouver des personnes intéressantes."
        )

    await update.message.reply_text(texte, reply_markup=clavier)
    return BIO


async def bio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_lang_from_context(context)
    bio = (update.message.text or "").strip()
    context.user_data["bio"] = bio

    if lang == "en":
        texte = (
            "Send a photo of yourself 📸\n\n"
            "For now, we accept just one photo. It will be visible to other users."
        )
    else:
        texte = (
            "Envoie une photo de toi 📸\n\n"
            "Pour l'instant on accepte une seule photo. Elle sera visible par les autres utilisateurs."
        )

    await update.message.reply_text(texte)
    return PHOTO


async def bio_skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = get_lang_from_context(context)

    context.user_data["bio"] = ""

    if lang == "en":
        texte = (
            "Send a photo of yourself 📸\n\n"
            "For now, we accept just one photo. It will be visible to other users."
        )
    else:
        texte = (
            "Envoie une photo de toi 📸\n\n"
            "Pour l'instant on accepte une seule photo. Elle sera visible par les autres utilisateurs."
        )

    await query.edit_message_text(texte)
    return PHOTO


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_lang_from_context(context)

    if not update.message.photo:
        if lang == "en":
            msg = "Please send a *photo*, not text 😊"
        else:
            msg = "Merci d'envoyer une *photo*, pas du texte 😊"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return PHOTO

    photo = update.message.photo[-1]
    context.user_data["id_photo"] = photo.file_id

    id_utilisateur = update.effective_user.id
    db = context.bot_data["bd"]

    profil_utilisateur: Dict[str, Any] = {
        "id_utilisateur": id_utilisateur,
        "username": update.effective_user.username or "",
        "nom": context.user_data.get("nom_fixe") or context.user_data.get("nom", ""),
        "age": context.user_data.get("age"),
        "genre": context.user_data.get("genre", ""),
        "cible_genre": context.user_data.get("cible_genre", ""),
        "pays": context.user_data.get("pays", ""),
        "ville": context.user_data.get("ville", ""),
        "bio": context.user_data.get("bio", ""),
        "id_photo": context.user_data.get("id_photo"),
        "latitude": context.user_data.get("latitude"),
        "longitude": context.user_data.get("longitude"),
        "langue": context.user_data.get("langue", "fr"),
    }

    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO utilisateurs 
            (id_utilisateur, username, nom, age, genre, cible_genre, pays, ville, bio, id_photo, latitude, longitude, langue)
            VALUES 
            (:id_utilisateur, :username, :nom, :age, :genre, :cible_genre, :pays, :ville, :bio, :id_photo, :latitude, :longitude, :langue)
            """,
            profil_utilisateur,
        )
        await db.commit()
        logger.info("Profil utilisateur %s sauvegardé avec succès.", id_utilisateur)

        if lang == "en":
            texte = (
                "Registration complete! ✨\n\n"
                "You can now start finding matches or view your profile."
            )
        else:
            texte = (
                "Inscription terminée ! ✨\n\n"
                "Tu peux maintenant commencer à chercher des correspondances ou voir ton profil."
            )

        await update.message.reply_text(
            texte,
            reply_markup=menu_principal_clavier(lang),
        )
        context.user_data.clear()
        return MENU

    except aiosqlite.Error as e:
        logger.error(
            "Erreur base de données lors de la sauvegarde du profil pour %s : %s",
            id_utilisateur,
            e,
        )
        if lang == "en":
            msg = "Sorry, an error occurred while saving your profile. Please try again later."
        else:
            msg = (
                "Désolé, une erreur s'est produite lors de la sauvegarde de ton profil. "
                "Veuillez réessayer plus tard."
            )
        await update.message.reply_text(msg)
        return ConversationHandler.END


# --- /myprofile : profil + menu 1..4 ---------------------------------------


async def myprofile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    id_utilisateur = update.effective_user.id
    db = context.bot_data["bd"]

    lang = await get_user_lang(update, context)

    async with db.execute(
        """
        SELECT nom, age, genre, cible_genre, ville, bio, id_photo 
        FROM utilisateurs 
        WHERE id_utilisateur = ?
        """,
        (id_utilisateur,),
    ) as cursor:
        profil = await cursor.fetchone()

    if not profil:
        if lang == "en":
            msg = "You don't have a profile yet. Send /start to create one."
        else:
            msg = "Tu n'as pas encore de profil. Envoie /start pour le créer."
        await update.message.reply_text(msg)
        return ConversationHandler.END

    nom, age, genre, cible_genre, ville, bio, id_photo = profil
    genre_aff = normaliser_genre_pour_affichage(genre, lang)
    cible_aff = normaliser_cible_pour_affichage(cible_genre, lang)

    if lang == "en":
        await update.message.reply_text("Here is your profile:")
    else:
        await update.message.reply_text("Voici ton profil :")

    if lang == "en":
        legende = (
            f"<b>📛 Name:</b> {nom or 'Not set'}\n"
            f"<b>🎂 Age:</b> {age} years\n"
            f"<b>⚧ Gender:</b> {genre_aff or 'Not set'}\n"
            f"<b>🎯 You are looking for:</b> {cible_aff or 'Not set'}\n"
            f"<b>🌍 Location:</b> {ville or 'Unknown city'}\n\n"
            f"<b>📝 Bio:</b>\n{bio or 'No bio yet.'}"
        )
    else:
        legende = (
            f"<b>📛 Nom :</b> {nom or 'Non renseigné'}\n"
            f"<b>🎂 Âge :</b> {age} ans\n"
            f"<b>⚧ Genre :</b> {genre_aff or 'Non renseigné'}\n"
            f"<b>🎯 Tu cherches :</b> {cible_aff or 'Non précisé'}\n"
            f"<b>🌍 Lieu :</b> {ville or 'Ville inconnue'}\n\n"
            f"<b>📝 Bio :</b>\n{bio or 'Pas encore de bio.'}"
        )

    await update.message.reply_photo(
        photo=id_photo, caption=legende, parse_mode=ParseMode.HTML
    )

    if lang == "en":
        texte_options = (
            "Choose an option:\n\n"
            "1. View profiles.\n"
            "2. Fill my profile again.\n"
            "3. Change my photo.\n"
            "4. Change my bio text."
        )
    else:
        texte_options = (
            "Choisis une option :\n\n"
            "1. Voir des profils.\n"
            "2. Remplir mon profil à nouveau.\n"
            "3. Modifier ma photo.\n"
            "4. Modifier le texte de mon profil."
        )

    clavier = ReplyKeyboardMarkup(
        [["1", "2"], ["3", "4"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(texte_options, reply_markup=clavier)
    await renvoyer_like_en_attente(update, context)
    return MYPROFILE_MENU


async def langage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /langage : permet de changer la langue de l'interface."""
    lang = await get_user_lang(update, context)

    if lang == "en":
        texte = "Choose the interface language:"
        fr_label = "🇫🇷 French"
        en_label = "🇬🇧 English"
    else:
        texte = "Choisis la langue de l'interface :"
        fr_label = "🇫🇷 Français"
        en_label = "🇬🇧 Anglais"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(fr_label, callback_data="setlang_fr"),
                InlineKeyboardButton(en_label, callback_data="setlang_en"),
            ]
        ]
    )

    await update.message.reply_text(texte, reply_markup=keyboard)
    await renvoyer_like_en_attente(update, context)


async def setlang_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite le choix de langue venant de /langage."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "setlang_en":
        lang = "en"
    else:
        lang = "fr"

    # 1. Mémoriser dans user_data
    context.user_data["langue"] = lang

    user_id = query.from_user.id
    db = context.bot_data.get("bd")

    # 2. Sauvegarder en base si le profil existe déjà
    exists = False
    if db is not None:
        await db.execute(
            "UPDATE utilisateurs SET langue = ? WHERE id_utilisateur = ?",
            (lang, user_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT 1 FROM utilisateurs WHERE id_utilisateur = ?",
            (user_id,),
        ) as cur:
            exists = (await cur.fetchone()) is not None

    # 3. Messages de confirmation
    if lang == "en":
        text_short = "Interface language changed to English 🇬🇧."
        if exists:
            followup = "You can now use the menu below."
        else:
            followup = "Send /start to create your profile."
    else:
        text_short = "Langue de l'interface changée en français 🇫🇷."
        if exists:
            followup = "Tu peux maintenant utiliser le menu ci-dessous."
        else:
            followup = "Envoie /start pour créer ton profil."

    # On remplace le message avec les boutons
    await query.edit_message_text(text_short)

    # Et on envoie (éventuellement) le menu principal
    if exists:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=followup,
            reply_markup=menu_principal_clavier(lang),
        )
    else:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=followup,
        )


async def myprofile_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choix = (update.message.text or "").strip()
    lang = await get_user_lang(update, context)

    # Si l'utilisateur appuie sur les boutons du menu principal alors qu'on est
    # encore dans MYPROFILE_MENU, on redirige proprement.
    if choix in ["Mon profil 👤", "My profile 👤"]:
        return await myprofile_command(update, context)

    if choix in ["Chercher une correspondance 💘", "Find a match 💘"]:
        return await chercher_correspondance(update, context)

    # Gestion normale 1..4
    if choix.startswith("1"):
        return await chercher_correspondance(update, context)

    if choix.startswith("2"):
        id_utilisateur = update.effective_user.id
        db = context.bot_data["bd"]
        await db.execute(
            "DELETE FROM utilisateurs WHERE id_utilisateur = ?",
            (id_utilisateur,),
        )
        await db.execute(
            "DELETE FROM swipes WHERE id_swipeur = ? OR id_swipe = ?",
            (id_utilisateur, id_utilisateur),
        )
        await db.commit()
        context.user_data.clear()

        if lang == "en":
            texte = "Let's fill your profile again.\n\nHow old are you?"
        else:
            texte = "On va remplir ton profil à nouveau.\n\nQuel âge as-tu ?"

        await update.message.reply_text(texte, reply_markup=ReplyKeyboardRemove())
        return AGE

    if choix.startswith("3"):
        if lang == "en":
            msg = "Send a new photo for your profile 📸"
        else:
            msg = "Envoie une nouvelle photo pour ton profil 📸"
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
        return EDITION_PHOTO

    if choix.startswith("4"):
        if lang == "en":
            msg = "Send the new text for your bio 📝"
        else:
            msg = "Envoie le nouveau texte de ta bio 📝"
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
        return EDITION_BIO

    # Si vraiment autre chose
    if lang == "en":
        msg = "Choose 1, 2, 3 or 4 using the buttons below."
    else:
        msg = "Choisis 1, 2, 3 ou 4 en utilisant les boutons en bas."
    await update.message.reply_text(msg)
    return MYPROFILE_MENU


async def sauvegarder_nouvelle_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = await get_user_lang(update, context)

    if not update.message.photo:
        if lang == "en":
            msg = "Please send a *photo*, not text 😊"
        else:
            msg = "Merci d'envoyer une *photo*, pas du texte 😊"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return EDITION_PHOTO

    id_utilisateur = update.effective_user.id
    nouveau_file_id = update.message.photo[-1].file_id
    db = context.bot_data["bd"]

    await db.execute(
        "UPDATE utilisateurs SET id_photo = ? WHERE id_utilisateur = ?",
        (nouveau_file_id, id_utilisateur),
    )
    await db.commit()

    if lang == "en":
        msg = "Your profile photo has been updated ✅"
    else:
        msg = "Ta photo de profil a été mise à jour ✅"

    await update.message.reply_text(
        msg,
        reply_markup=menu_principal_clavier(lang),
    )
    return MENU


# --- Recherche de correspondances ------------------------------------------


async def chercher_correspondance(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    id_utilisateur = update.effective_user.id
    db = context.bot_data["bd"]
    lang = await get_user_lang(update, context)

    user_chat = update.effective_user
    telegram_username = user_chat.username or ""

    # Mettre à jour le username en base
    await db.execute(
        "UPDATE utilisateurs SET username = ? WHERE id_utilisateur = ?",
        (telegram_username, id_utilisateur),
    )
    await db.commit()

    # Si l'utilisateur n'a pas de username, on bloque la recherche
    if not telegram_username:
        if lang == "en":
            msg = (
                "To find matches, you need to set a Telegram username first.\n\n"
                "Open Telegram → Settings → Edit profile → Username and choose an @name.\n"
                "Then come back here and try again 🙂"
            )
        else:
            msg = (
                "Pour chercher des correspondances, tu dois d'abord définir un "
                "nom d'utilisateur Telegram (@username).\n\n"
                "Ouvre Telegram → Paramètres → Modifier le profil → Nom d'utilisateur "
                "et choisis un @pseudo.\n"
                "Reviens ensuite ici et relance la recherche 🙂"
            )
        await update.effective_message.reply_text(
            msg,
            reply_markup=menu_principal_clavier(lang),
        )
        return MENU

    # 1. Récupérer les préférences de l'utilisateur (genre + cible_genre + ville + pays)
    async with db.execute(
        "SELECT genre, cible_genre, ville, pays FROM utilisateurs WHERE id_utilisateur = ?",
        (id_utilisateur,),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        # Normalement ne devrait pas arriver, mais au cas où
        if lang == "en":
            msg = "You don't have a profile yet. Send /start to create one."
        else:
            msg = "Tu n'as pas encore de profil. Envoie /start pour le créer."
        await update.effective_message.reply_text(msg)
        return MENU

    user_genre, user_target, user_city, user_country = row

    # 2. Conditions de base (sans filtre géographique)
    base_conditions = [
        "id_utilisateur != ?",
        """id_utilisateur NOT IN (
               SELECT id_swipe FROM swipes 
               WHERE id_swipeur = ? 
                 AND date_swipe >= datetime('now','-1 day')
           )""",
        "username IS NOT NULL AND username != ''",
    ]
    base_params = [id_utilisateur, id_utilisateur]

    # Filtre selon la cible recherchée
    if user_target in ("Femmes", "Women"):
        base_conditions.append("genre IN ('Femme','Woman')")
    elif user_target in ("Hommes", "Men"):
        base_conditions.append("genre IN ('Homme','Man')")
    # "Peu importe" / "Doesn't matter" => pas de filtre de genre

    async def fetch_match(extra_condition: Optional[str] = None, extra_param: Optional[Any] = None):
        conditions = list(base_conditions)
        params = list(base_params)
        if extra_condition:
            conditions.append(extra_condition)
            params.append(extra_param)
        where_clause = " AND ".join(conditions)
        requete = f"""
            SELECT id_utilisateur, nom, genre, age, ville, bio, id_photo
            FROM utilisateurs
            WHERE {where_clause}
            ORDER BY RANDOM()
            LIMIT 1
        """
        async with db.execute(requete, params) as cursor:
            return await cursor.fetchone()

    # 3. On tente d'abord même ville, puis même pays
    correspondance_potentielle = None

    if user_city:
        correspondance_potentielle = await fetch_match(
            "LOWER(ville) = LOWER(?)", user_city
        )

    if not correspondance_potentielle and user_country:
        correspondance_potentielle = await fetch_match(
            "pays = ?", user_country
        )

    # Si on ne connaît ni ville ni pays (anciens profils), on garde le fallback global
    if not correspondance_potentielle and not (user_city or user_country):
        correspondance_potentielle = await fetch_match()

    message_obj = update.effective_message

    if correspondance_potentielle:
        (
            id_match,
            nom,
            genre,
            age,
            ville,
            bio,
            id_photo,
        ) = correspondance_potentielle
        genre_aff = normaliser_genre_pour_affichage(genre, lang)
        context.user_data["id_correspondance_potentielle"] = id_match

        if lang == "en":
            legende = (
                f"<b>{nom or 'User'}, {age} years</b>\n"
                f"<b>⚧ Gender:</b> {genre_aff or 'Not set'}\n"
                f"<b>📍 Location:</b> {ville or 'Unknown city'}\n\n"
                f"<b>📝 Bio:</b>\n{bio or 'No description.'}"
            )
            texte_suivi = "Use the buttons below to like or skip 😊"
        else:
            legende = (
                f"<b>{nom or 'Utilisateur'}, {age} ans</b>\n"
                f"<b>⚧ Genre :</b> {genre_aff or 'Non renseigné'}\n"
                f"<b>📍 Lieu :</b> {ville or 'Ville inconnue'}\n\n"
                f"<b>📝 Bio :</b>\n{bio or 'Pas de description.'}"
            )
            texte_suivi = "Utilise les boutons ci-dessous pour liker ou passer 😊"

        await message_obj.reply_photo(
            photo=id_photo,
            caption=legende,
            parse_mode=ParseMode.HTML,
        )
        await message_obj.reply_text(
            texte_suivi,
            reply_markup=clavier_recherche_match(lang),
        )
        return RECHERCHE_MATCH

    else:
        await envoyer_message_partage_plus_tard(message_obj, context)
        return MENU


# --- Helper : message de match mutuel avec profil + nom cliquable ----------


async def envoyer_match_mutuel(
    context: ContextTypes.DEFAULT_TYPE,
    db: aiosqlite.Connection,
    destinataire_id: int,
    autre_id: int,
):
    """
    Envoie à destinataire_id le profil complet de autre_id
    + un message de match mutuel avec un nom cliquable qui ouvre le chat.
    """

    # Langue du destinataire
    async with db.execute(
        "SELECT langue FROM utilisateurs WHERE id_utilisateur = ?",
        (destinataire_id,),
    ) as cur:
        row_lang = await cur.fetchone()
    lang = row_lang[0] if row_lang and row_lang[0] in ("fr", "en") else "fr"

    # Profil de l'autre personne
    async with db.execute(
        """
        SELECT nom, age, genre, ville, bio, id_photo, username
        FROM utilisateurs
        WHERE id_utilisateur = ?
        """,
        (autre_id,),
    ) as cur:
        profil = await cur.fetchone()

    if not profil:
        # Fallback très simple si jamais le profil n'existe plus
        if lang == "en":
            txt = "🎉 Mutual match! You can now write to this person."
        else:
            txt = "🎉 Match mutuel ! Tu peux maintenant écrire à cette personne."
        await context.bot.send_message(chat_id=destinataire_id, text=txt)
        return

    nom, age, genre, ville, bio, id_photo, username = profil
    genre_aff = normaliser_genre_pour_affichage(genre, lang)

    # Nom affiché
    if nom:
        display_name = nom
    else:
        display_name = "Utilisateur" if lang == "fr" else "User"

    # Lien cliquable basé sur username ou id
    if username:
        href = f"https://t.me/{username}"
    else:
        href = f"tg://user?id={autre_id}"

    link_html = f'<a href="{href}">{display_name}</a>'

    if lang == "en":
        caption = (
            f"<b>{display_name}, {age} years</b>\n"
            f"<b>⚧ Gender:</b> {genre_aff or 'Not set'}\n"
            f"<b>📍 Location:</b> {ville or 'Unknown city'}\n\n"
            f"<b>📝 Bio:</b>\n{bio or 'No description.'}\n\n"
            f"🎉 Mutual match!\nYou can now write to {link_html}."
        )
    else:
        caption = (
            f"<b>{display_name}, {age} ans</b>\n"
            f"<b>⚧ Genre :</b> {genre_aff or 'Non renseigné'}\n"
            f"<b>📍 Lieu :</b> {ville or 'Ville inconnue'}\n\n"
            f"<b>📝 Bio :</b>\n{bio or 'Pas de description.'}\n\n"
            f"🎉 Match mutuel !\nTu peux maintenant écrire à {link_html}."
        )

    # Envoi du profil avec le message de match
    try:
        await context.bot.send_photo(
            chat_id=destinataire_id,
            photo=id_photo,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        # Fallback sans photo
        await context.bot.send_message(
            chat_id=destinataire_id,
            text=caption,
            parse_mode=ParseMode.HTML,
        )


# --- Réactions via clavier (❤️ / ❌) ----------------------------------------


async def aimer_match_texte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await reaction_match_texte(update, context, "aimer")


async def passer_match_texte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await reaction_match_texte(update, context, "passer")


async def reaction_match_texte(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
) -> int:
    id_utilisateur = update.effective_user.id
    db = context.bot_data["bd"]
    message = update.message
    lang = await get_user_lang(update, context)

    id_match = context.user_data.get("id_correspondance_potentielle")
    if not id_match:
        if lang == "en":
            msg = "There is no current profile. Use 'Find a match 💘'."
        else:
            msg = "Il n'y a pas de profil en cours. Utilise 'Chercher une correspondance 💘'."
        await message.reply_text(
            msg,
            reply_markup=menu_principal_clavier(lang),
        )
        return MENU

    # Limite 15 réactions / 24h
    async with db.execute(
        """
        SELECT COUNT(*) FROM swipes 
        WHERE id_swipeur = ? 
          AND date_swipe >= datetime('now','-1 day')
        """,
        (id_utilisateur,),
    ) as cursor:
        nb = (await cursor.fetchone())[0]

    if nb >= 15:
        await envoyer_message_limite(message, context)
        return MENU

    # Enregistrer le swipe (like ou passer)
    try:
        await db.execute(
            "INSERT INTO swipes (id_swipeur, id_swipe, action) VALUES (?, ?, ?)",
            (id_utilisateur, id_match, action),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        # L'utilisateur a déjà interagi avec ce profil
        if lang == "en":
            msg = "You have already interacted with this profile."
        else:
            msg = "Tu as déjà interagi avec ce profil."
        logger.warning(
            "L'utilisateur %s a essayé de swiper à nouveau %s.",
            id_utilisateur,
            id_match,
        )
        await message.reply_text(msg)
        return await chercher_correspondance(update, context)

    if action == "aimer":
        # Vérifier si l'autre a déjà liké AVANT
        async with db.execute(
            """
            SELECT 1 FROM swipes 
            WHERE id_swipeur = ? AND id_swipe = ? AND action = 'aimer'
            """,
            (id_match, id_utilisateur),
        ) as cursor:
            est_mutuel = (await cursor.fetchone()) is not None

        pending = context.bot_data.setdefault("pending_likes", {})

        if est_mutuel:
            # Déjà un like de l'autre côté -> match direct
            pending.pop(id_utilisateur, None)
            pending.pop(id_match, None)
            try:
                await envoyer_match_mutuel(context, db, id_utilisateur, id_match)
                await envoyer_match_mutuel(context, db, id_match, id_utilisateur)
            except Exception as e:
                logger.warning("Erreur lors de l'envoi des messages de match : %s", e)
        else:
            # Premier like dans la paire -> notification "quelqu'un a liké ton profil"
            if lang == "en":
                notif_text = "You received a new like ❤️\n\nDo you want to see the profile?"
            else:
                notif_text = "Tu as reçu un nouveau like ❤️\n\nVeux-tu voir le profil ?"

            notif_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Voir le profil / See profile",
                            callback_data=f"notif_like_{id_utilisateur}",
                        )
                    ]
                ]
            )

            pending[id_match] = id_utilisateur

            try:
                await context.bot.send_message(
                    chat_id=id_match,
                    text=notif_text,
                    reply_markup=notif_keyboard,
                )
            except Exception as e:
                logger.warning(
                    "Impossible d'envoyer une notification de like à %s : %s",
                    id_match,
                    e,
                )

        # On ne renvoie pas de "Tu as aimé ce profil", on passe direct au suivant.
    else:
        # action == "passer" : rien à afficher, on passe au profil suivant
        pass

    return await chercher_correspondance(update, context)


# --- Notification & gestion des likes (via inline boutons) ------------------


async def notif_like_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Quand l'utilisateur clique sur 'Voir le profil / See profile' dans la notif de like."""
    query = update.callback_query
    await query.answer()

    lang = await get_user_lang(update, context)
    db = context.bot_data["bd"]

    # callback_data = "notif_like_<id_likeur>"
    _, _, id_likeur_str = query.data.split("_")
    id_likeur = int(id_likeur_str)

    user_id = query.from_user.id

    # Ce like n'est plus "en attente" pour cet utilisateur
    pending = context.bot_data.get("pending_likes", {})
    pending.pop(user_id, None)

    # Est-ce que l'utilisateur a déjà LIKÉ ce profil auparavant ?
    async with db.execute(
        """
        SELECT 1 FROM swipes
        WHERE id_swipeur = ? AND id_swipe = ? AND action = 'aimer'
        """,
        (user_id, id_likeur),
    ) as cur:
        deja_aime = (await cur.fetchone()) is not None

    # On récupère le profil de la personne qui a liké
    async with db.execute(
        """
        SELECT nom, genre, age, ville, bio, id_photo
        FROM utilisateurs
        WHERE id_utilisateur = ?
        """,
        (id_likeur,),
    ) as cursor:
        profil = await cursor.fetchone()

    if not profil:
        if lang == "en":
            msg = "This profile is no longer available."
        else:
            msg = "Le profil de cette personne n'est plus disponible."
        await query.edit_message_text(msg)
        return MENU

    nom, genre, age, ville, bio, id_photo = profil
    genre_aff = normaliser_genre_pour_affichage(genre, lang)

    if lang == "en":
        legende = (
            f"<b>{nom or 'User'}, {age} years</b>\n"
            f"<b>⚧ Gender:</b> {genre_aff or 'Not set'}\n"
            f"<b>📍 Location:</b> {ville or 'Unknown city'}\n\n"
            f"<b>📝 Bio:</b>\n{bio or 'No description.'}"
        )
        titre = "Here is the profile of the person who liked you:"
    else:
        legende = (
            f"<b>{nom or 'Utilisateur'}, {age} ans</b>\n"
            f"<b>⚧ Genre :</b> {genre_aff or 'Non renseigné'}\n"
            f"<b>📍 Lieu :</b> {ville or 'Ville inconnue'}\n\n"
            f"<b>📝 Bio :</b>\n{bio or 'Pas de description.'}"
        )
        titre = "Voici le profil de la personne qui t'a liké :"

    # Clavier uniquement si l'utilisateur n'a PAS encore liké cette personne
    if not deja_aime:
        clavier = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ Passer / Skip", callback_data=f"match_passer_{id_likeur}"
                    ),
                    InlineKeyboardButton(
                        "❤️ J'aime / Like", callback_data=f"match_aimer_{id_likeur}"
                    ),
                ]
            ]
        )
    else:
        clavier = None  # déjà liké -> pas de boutons

    await query.edit_message_text(titre)

    try:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=id_photo,
            caption=legende,
            parse_mode=ParseMode.HTML,
            reply_markup=clavier,
        )
    except Exception as e:
        logger.warning("Erreur en envoyant le profil via notif_like : %s", e)
        # Fallback sans photo
        if lang == "en":
            msg = (
                "I couldn't display the photo for this profile, "
                "but you can still like or skip this person."
            )
        else:
            msg = (
                "Impossible d'afficher la photo pour ce profil, "
                "mais tu peux quand même aimer ou passer cette personne."
            )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=msg,
            reply_markup=clavier,
        )

    return RECHERCHE_MATCH


async def renvoyer_like_en_attente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Si l'utilisateur a un like en attente dans context.bot_data["pending_likes"],
    on lui renvoie une notification avec le bouton 'Voir le profil'.
    """
    pending = context.bot_data.get("pending_likes", {})
    user_id = update.effective_user.id
    id_likeur = pending.get(user_id)

    if not id_likeur:
        return  # aucun like en attente

    lang = await get_user_lang(update, context)

    if lang == "en":
        notif_text = "You received a new like ❤️\n\nDo you want to see the profile?"
    else:
        notif_text = "Tu as reçu un nouveau like ❤️\n\nVeux-tu voir le profil ?"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Voir le profil / See profile",
                    callback_data=f"notif_like_{id_likeur}",
                )
            ]
        ]
    )

    await context.bot.send_message(
        chat_id=user_id,
        text=notif_text,
        reply_markup=keyboard,
    )


async def choix_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gestion des likes/pass via boutons inline (notif_like, anciens messages)."""
    query = update.callback_query
    await query.answer()
    lang = await get_user_lang(update, context)

    id_utilisateur = update.effective_user.id
    db = context.bot_data["bd"]

    # Limite 15 réactions / 24h
    async with db.execute(
        """
        SELECT COUNT(*) FROM swipes 
        WHERE id_swipeur = ? 
          AND date_swipe >= datetime('now','-1 day')
        """,
        (id_utilisateur,),
    ) as cursor:
        nb = (await cursor.fetchone())[0]

    if nb >= 15:
        await envoyer_message_limite(query.message, context)
        return MENU

    # callback_data = "match_aimer_123456" ou "match_passer_123456"
    action, id_match_str = query.data.split("_")[1:]
    id_match = int(id_match_str)

    try:
        await db.execute(
            "INSERT INTO swipes (id_swipeur, id_swipe, action) VALUES (?, ?, ?)",
            (id_utilisateur, id_match, action),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        if lang == "en":
            msg = "You have already interacted with this profile."
        else:
            msg = "Tu as déjà interagi avec ce profil."
        logger.warning(
            "L'utilisateur %s a essayé de swiper à nouveau %s.",
            id_utilisateur,
            id_match,
        )
        await query.edit_message_text(msg)
        return await chercher_correspondance(update, context)

    if action == "aimer":
        # Vérifier si like mutuel déjà existant
        async with db.execute(
            """
            SELECT 1 FROM swipes 
            WHERE id_swipeur = ? AND id_swipe = ? AND action = 'aimer'
            """,
            (id_match, id_utilisateur),
        ) as cursor:
            est_mutuel = (await cursor.fetchone()) is not None

        pending = context.bot_data.setdefault("pending_likes", {})

        if est_mutuel:
            # Match direct, pas de notif "quelqu'un a liké"
            pending.pop(id_utilisateur, None)
            pending.pop(id_match, None)
            try:
                await envoyer_match_mutuel(context, db, id_utilisateur, id_match)
                await envoyer_match_mutuel(context, db, id_match, id_utilisateur)
            except Exception as e:
                logger.warning("Erreur lors de l'envoi des messages de match : %s", e)
        else:
            # Premier like dans la paire -> notif
            if lang == "en":
                notif_text = "You received a new like ❤️\n\nDo you want to see the profile?"
            else:
                notif_text = "Tu as reçu un nouveau like ❤️\n\nVeux-tu voir le profil ?"

            notif_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Voir le profil / See profile",
                            callback_data=f"notif_like_{id_utilisateur}",
                        )
                    ]
                ]
            )

            pending[id_match] = id_utilisateur

            try:
                await context.bot.send_message(
                    chat_id=id_match,
                    text=notif_text,
                    reply_markup=notif_keyboard,
                )
            except Exception as e:
                logger.warning(
                    "Impossible d'envoyer une notification de like à %s : %s",
                    id_match,
                    e,
                )

        # On ne modifie plus la légende (pas de 'Tu as aimé ce profil')
    else:
        # action == "passer"
        # On peut éventuellement marquer dans la légende, mais tu avais demandé de simplifier,
        # donc on ne rajoute pas de texte, on passe juste au profil suivant.
        pass

    return await chercher_correspondance(update, context)


# --- Édition de profil (bio seule) -----------------------------------------


async def sauvegarder_nouvelle_bio(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = await get_user_lang(update, context)
    id_utilisateur = update.effective_user.id
    nouvelle_bio = (update.message.text or "").strip()
    db = context.bot_data["bd"]

    await db.execute(
        "UPDATE utilisateurs SET bio = ? WHERE id_utilisateur = ?",
        (nouvelle_bio, id_utilisateur),
    )
    await db.commit()

    if lang == "en":
        msg = "Your bio has been updated successfully!"
    else:
        msg = "Ta bio a été mise à jour avec succès !"

    await update.message.reply_text(
        msg,
        reply_markup=menu_principal_clavier(lang),
    )
    return MENU


# --- Menu principal ---------------------------------------------------------


async def menu_principal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texte = update.message.text or ""
    lang = await get_user_lang(update, context)

    if texte in ["Chercher une correspondance 💘", "Find a match 💘"]:
        return await chercher_correspondance(update, context)
    elif texte in ["Mon profil 👤", "My profile 👤"]:
        return await myprofile_command(update, context)
    else:
        if lang == "en":
            msg = "Invalid choice. Please use the buttons below."
        else:
            msg = "Choix invalide. Veuillez utiliser les boutons ci-dessous."
        await update.message.reply_text(msg)
        return MENU


# --- Fonctions générales & fallback ----------------------------------------


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await get_user_lang(update, context)
    if lang == "en":
        msg = "Process cancelled. Type /start to begin again."
    else:
        msg = "Processus annulé. Tape /start pour recommencer."
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def commande_inconnue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await get_user_lang(update, context)
    if lang == "en":
        msg = "Sorry, I didn't understand this command. Try /start."
    else:
        msg = "Désolé, je n'ai pas compris cette commande. Essaie /start."
    await update.message.reply_text(msg)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Ton ID Telegram est : <code>{user.id}</code>",
        parse_mode=ParseMode.HTML,
    )
    await renvoyer_like_en_attente(update, context)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Statistiques globales du bot (réservé à l'admin)."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Cette commande est réservée à l'administrateur.")
        return

    db = context.bot_data["bd"]

    # Nombre total d'utilisateurs
    async with db.execute("SELECT COUNT(*) FROM utilisateurs") as cur:
        total_users = (await cur.fetchone())[0]

    # Nouveaux utilisateurs aujourd'hui
    async with db.execute(
        "SELECT COUNT(*) FROM utilisateurs "
        "WHERE date_inscription >= date('now','start of day')"
    ) as cur:
        new_today = (await cur.fetchone())[0]

    # Likes totaux
    async with db.execute(
        "SELECT COUNT(*) FROM swipes WHERE action = 'aimer'"
    ) as cur:
        total_likes = (await cur.fetchone())[0]

    # Likes sur les 24 dernières heures
    async with db.execute(
        "SELECT COUNT(*) FROM swipes "
        "WHERE action = 'aimer' "
        "AND date_swipe >= datetime('now','-1 day')"
    ) as cur:
        likes_24h = (await cur.fetchone())[0]

    # Matchs (likes mutuels)
    async with db.execute(
        """
        SELECT COUNT(*) 
        FROM (
            SELECT a.id_swipeur, a.id_swipe
            FROM swipes a
            JOIN swipes b
              ON a.id_swipeur = b.id_swipe
             AND a.id_swipe   = b.id_swipeur
            WHERE a.action = 'aimer'
              AND b.action = 'aimer'
              AND a.id_swipeur < a.id_swipe
        )
        """
    ) as cur:
        nb_matchs = (await cur.fetchone())[0]

    texte = (
        "📊 <b>Statistiques du bot</b>\n\n"
        f"👥 Utilisateurs inscrits : <b>{total_users}</b>\n"
        f"🆕 Nouveaux aujourd'hui : <b>{new_today}</b>\n\n"
        f"❤️ Likes totaux : <b>{total_likes}</b>\n"
        f"❤️ Likes sur 24h : <b>{likes_24h}</b>\n"
        f"🔗 Matchs (likes mutuels) : <b>{nb_matchs}</b>\n"
    )

    await update.message.reply_text(texte, parse_mode=ParseMode.HTML)
    await renvoyer_like_en_attente(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /help : indique comment contacter l'assistant."""
    lang = await get_user_lang(update, context)

    username_clean = SUPPORT_USERNAME.lstrip("@") if SUPPORT_USERNAME else ""
    if username_clean:
        contact_link = f"https://t.me/{username_clean}"

        if lang == "en":
            text = (
                "🆘 <b>Need help?</b>\n\n"
                "If you have a problem with the bot or a question, you can contact the assistant here:\n\n"
                f"👉 @{username_clean}\n\n"
                "Tap the button below to open the chat."
            )
            button_text = "Contact support"
        else:
            text = (
                "🆘 <b>Besoin d'aide ?</b>\n\n"
                "Si tu as un problème avec le bot ou une question, tu peux contacter l'assistant ici :\n\n"
                f"👉 @{username_clean}\n\n"
                "Appuie sur le bouton ci-dessous pour ouvrir le chat."
            )
            button_text = "Contacter l'assistant"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(button_text, url=contact_link)]]
        )
    else:
        # Au cas où SUPPORT_USERNAME n'est pas encore configuré
        if lang == "en":
            text = (
                "🆘 <b>Need help?</b>\n\n"
                "The support contact is not configured yet.\n"
                "Please contact the bot administrator directly."
            )
        else:
            text = (
                "🆘 <b>Besoin d'aide ?</b>\n\n"
                "Le contact support n'est pas encore configuré.\n"
                "Merci de contacter directement l'administrateur du bot."
            )
        keyboard = None

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await renvoyer_like_en_attente(update, context)


# --- Lancement du bot -------------------------------------------------------


def principal() -> None:
    # Récupérer le token depuis les variables d'environnement
    token = "8356135292:AAEp4ZFxbmsfed23pynxv_Zd8sGqgjFUgKc"
    
    if not token:
        logger.error("=" * 60)
        logger.error("❌ ERREUR : TOKEN MANQUANT !")
        logger.error("=" * 60)
        return

    logger.info("✅ Token trouvé. Démarrage du bot...")
    
    # Construction de l'application
    application = (
        Application.builder()
        .token(token)
        .post_init(initialiser_bd)
        .post_shutdown(fermer_bd)
        .build()
    )

    # Configuration des handlers
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", demarrage)],
        states={
            LANGUAGE: [CallbackQueryHandler(language_handler, pattern="^lang_")],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_handler)],
            GENRE: [CallbackQueryHandler(genre_handler, pattern="^gender_")],
            TARGET: [CallbackQueryHandler(target_handler, pattern="^target_")],
            CHOIX_LOCALISATION: [
                MessageHandler(
                    filters.LOCATION | (filters.TEXT & ~filters.COMMAND),
                    choix_localisation_handler,
                )
            ],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_handler)],
            CHOIX_VILLE_PRECISE: [
                MessageHandler(
                    filters.LOCATION | (filters.TEXT & ~filters.COMMAND),
                    choix_ville_precise_handler,
                )
            ],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_handler)],
            BIO: [
                CallbackQueryHandler(bio_skip_handler, pattern="^bio_skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, bio_handler),
            ],
            PHOTO: [MessageHandler(filters.PHOTO, photo_handler)],
            MENU: [
                MessageHandler(
                    filters.Regex("^(Chercher une correspondance 💘|Find a match 💘)$"),
                    chercher_correspondance,
                ),
                MessageHandler(
                    filters.Regex("^(Mon profil 👤|My profile 👤)$"),
                    myprofile_command,
                ),
            ],
            RECHERCHE_MATCH: [
                MessageHandler(filters.Regex("^(❤️ J'aime|❤️ Like)$"), aimer_match_texte),
                MessageHandler(filters.Regex("^(❌ Passer|❌ Skip)$"), passer_match_texte),
                CallbackQueryHandler(choix_match, pattern="^match_"),
            ],
            EDITION_BIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sauvegarder_nouvelle_bio)
            ],
            MYPROFILE_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, myprofile_choice)
            ],
            EDITION_PHOTO: [
                MessageHandler(filters.PHOTO, sauvegarder_nouvelle_photo),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    lambda update, context: update.message.reply_text(
                        "Envoie une *photo* pour mettre à jour ton profil 📸",
                        parse_mode="Markdown",
                    ),
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", annuler),
            CommandHandler("start", demarrage),
            CommandHandler("myprofile", myprofile_command),
            CallbackQueryHandler(notif_like_handler, pattern="^notif_like_"),
            CallbackQueryHandler(bio_skip_handler, pattern="^bio_skip$"),
        ],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("myprofile", myprofile_command))
    application.add_handler(CommandHandler("langage", langage_command))
    application.add_handler(CallbackQueryHandler(setlang_handler, pattern="^setlang_"))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(notif_like_handler, pattern="^notif_like_"))
    application.add_handler(CallbackQueryHandler(choix_match, pattern="^match_"))
    application.add_handler(MessageHandler(filters.COMMAND, commande_inconnue))
    
    logger.info("🚀 Bot démarré avec succès!")
    
    application.run_polling()