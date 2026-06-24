#!/usr/bin/env python3
"""
Bot Telegram B-Roll — assistant de production vidéo YouTube.

Flux STEP-BY-STEP : script → AI segmente → on traite UN segment à la fois.
Chaque proposition (image ou vidéo) reçoit un ID unique #N que tu peux flagger
pour la conserver. /suivant pour passer au segment suivant.

Commandes :
  /script        → le prochain message est un script (sinon : colle le texte directement)
  /suivant       → segment suivant
  /garde N [N…]  → conserver les propositions #N
  /retire N [N…] → retirer de la sélection
  /conserves     → liste des propositions conservées
  /changer       → d'autres images pour le segment courant
  /id /help

Mode POLLING : appels sortants uniquement, aucune URL publique requise.
"""
import os
import re
import io
import sys
import json
import time
import base64
import shutil
import zipfile
import tempfile
import subprocess
import mimetypes
from urllib.parse import urlparse

import requests

TG_TOKEN = os.environ["BROLL_BOT_TOKEN"]
ALLOWED = {c.strip() for c in os.environ.get("BROLL_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TG_TOKEN}"

CHUTES_KEY = os.environ.get("CHUTES_API_KEY", "")
CHUTES_BASE = os.environ.get("CHUTES_BASE_URL", "https://llm.chutes.ai/v1").rstrip("/")
CHUTES_MODEL = os.environ.get("CHUTES_MODEL", "Qwen/Qwen3-32B-TEE")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://host.docker.internal:18080").rstrip("/")
REMOTION_URL = os.environ.get("REMOTION_URL", "http://broll-remotion:8099").rstrip("/")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
EXPORT_DIR = os.environ.get("EXPORT_DIR", "/exports")
EXPORT_BASE_URL = os.environ.get("EXPORT_BASE_URL", "http://localhost:8088").rstrip("/")
MIN_WIDTH = int(os.environ.get("BROLL_MIN_WIDTH", "1000"))
RUSHS_DIR = os.environ.get("RUSHS_DIR", "/rushs")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:9000").rstrip("/")
WHISPER_LANG = os.environ.get("WHISPER_LANGUAGE", "fr")
RUSH_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm")

# Filtrage technique des images (rejet du déchet)
ICON_ENGINES = ("lucide", "iconify", "material", "icons")
JUNK_DOMAINS = ("cdn.jsdelivr.net", "flaticon", "iconfinder", "icons8",
                "fonts.gstatic", "freepik", "vecteezy", "clipart")
# Banques d'images qui servent des previews filigranés → inexploitables en b-roll
WATERMARK_DOMAINS = ("shutterstock", "gettyimages", "istockphoto", "istock",
                     "alamy", "dreamstime", "depositphotos", "123rf", "stock.adobe",
                     "fotolia", "bigstock", "canstockphoto", "vectorstock",
                     "agefotostock", "stocklib", "imago-images", "stockphoto",
                     "stock-photo", "stock-image", "lookphotos", "photononstop")
# Motifs d'URL typiques d'un aperçu filigrané
WATERMARK_PATTERNS = ("watermark", "/comp/", "_wm", "-wm.", "/preview")

HELP = (
    "🎬 Bot B-Roll YouTube — flux pas à pas\n\n"
    "Envoie ton script (texte ou fichier .txt) : je demande combien de segments tu veux, "
    "puis je propose des b-rolls SEGMENT PAR SEGMENT. Chaque proposition a un ID #N.\n"
    "Tu peux renvoyer un nouveau bout de script à tout moment : il s'AJOUTE au projet.\n\n"
    "📁 PROJETS (chacun = un dossier dans la galerie, IDs/segments relatifs) :\n"
    "/new [nom]     → nouveau projet · /projets → liste · /projet <slug> → basculer\n\n"
    "🎞️ DÉRUSH — télécharge puis dérush :\n"
    "/rushs         → liste les rushs · /derush <nom> → 1re version dérushée\n"
    "(gros fichiers : rsync -P --inplace <fichier> ubuntu@<serveur>:~/rushs/)\n\n"
    "/garde N [N…]  → conserver les propositions #N\n"
    "/retire N [N…] → retirer de la sélection\n"
    "/conserves     → voir ce que tu as conservé\n"
    "/suivant       → passer au segment suivant\n"
    "/plus [N]      → générer N segments de plus à partir du script\n"
    "/cherche TXT   → relancer la recherche du segment avec tes mots\n"
    "/changer       → d'autres images pour le segment courant\n"
    "/graphiques [N]→ repérer les moments à illustrer par un GRAPHIQUE et les générer\n"
    "/anim TXT      → GRAPHIQUE(S) ANIMÉ(S) : 2-3 variantes en MP4 (vidéo + fichier)\n"
    "/anims [N]     → BATCH : anime tous les moments chiffrés du script\n"
    "/custom TXT    → animation SUR MESURE : le LLM code une anim spécifique (ex: chaîne de maillons)\n"
    "/modcustom TXT → MODIFIER l'anim sur mesure courante sans repartir de zéro\n"
    "/miniatures [sujet] → 10 idées de miniatures YouTube (titres + images IA) pour t'inspirer\n"
    "/schema TXT    → diagramme/schéma statique (mermaid)\n"
    "/modif TXT     → modifier le dernier diagramme\n"
    "/export        → images HD + vidéos (liens) + ZIP, servis par Caddy (tunnel 8088)\n"
    "/autoanim      → activer/couper les animations auto dans le flux\n"
    "/reset         → vider le projet courant (segments + sélection)\n"
    "/script TXT    → analyser ce script (ou /script seul puis colle)\n"
    "/id /help"
)

# ---- Prompts ----
SEGMENT_SYSTEM = (
    "Tu es directeur artistique pour une chaîne YouTube tech/dev.\n"
    "On te donne un script. Identifie 8 à 12 moments clés à illustrer en b-roll.\n\n"
    "RÈGLE D'OR : n'illustre JAMAIS la phrase au sens littéral. Trouve un visuel que le "
    "SPECTATEUR RECONNAÎT au premier coup d'œil et qui représente le sujet. "
    "Demande-toi : « qu'est-ce que le public identifie immédiatement ici ? »\n\n"
    "Hiérarchie des bons visuels (du plus fort au plus faible) :\n"
    "  1. VISAGES de personnalités connues liées au sujet (fondateurs, dirigeants, figures publiques). "
    "Ex : OpenAI → Sam Altman ; Anthropic → Dario Amodei ; Tesla → Elon Musk.\n"
    "  2. LOGOS de marques/produits identifiables.\n"
    "  3. PRODUITS ou INTERFACES reconnaissables (l'app ChatGPT, l'écran de Claude, un iPhone…).\n"
    "  4. LIEUX VRAIMENT iconiques (un siège emblématique reconnaissable, pas un immeuble anonyme).\n\n"
    "INTERDIT : tout visuel générique que personne ne reconnaît (un bâtiment lambda, une "
    "« vue extérieure » anonyme, une foule floue, une poignée de main corporate, une image d'illustration creuse).\n\n"
    "Pour chaque moment :\n"
    "  • extrait : cite l'extrait exact du script (max 15 mots)\n"
    "  • type : 'image' | 'video' (extrait YouTube : interview, keynote, démo) | 'ia' (concept abstrait sans visuel reconnaissable)\n"
    "  • queries : 2 à 3 requêtes EN ANGLAIS couvrant des ANGLES reconnaissables DIFFÉRENTS "
    "(ex : ['Sam Altman portrait', 'OpenAI logo', 'Dario Amodei Anthropic'])\n"
    "  • visuel : décris le visuel reconnaissable choisi ET pourquoi le public l'identifie\n\n"
    'Réponds UNIQUEMENT en JSON : {"segments": [{"n": 1, "extrait": "...", '
    '"type": "image|video|ia", "queries": ["...", "...", "..."], "visuel": "..."}]}'
)

TIMESTAMP_SYSTEM = (
    "Tu es un assistant de montage vidéo.\n"
    "On te donne les métadonnées d'une vidéo YouTube (titre, durée, description, chapitres) "
    "et la description d'un b-roll à illustrer.\n"
    "Sélectionne la fenêtre de 10 à 25 secondes la plus pertinente.\n"
    "Si la vidéo ne correspond pas du tout, indique pertinent=false.\n"
    'Réponds UNIQUEMENT en JSON : {"start": <s>, "end": <s>, "pertinent": true|false, "raison": "..."}'
)

DIAGRAM_SYSTEM = (
    "Tu produis des diagrammes pour des vidéos tech/dev. Choisis le MEILLEUR outil :\n"
    "  • 'mermaid' pour les FLUX, processus, architectures, relations, séquences, "
    "organigrammes, timelines, arbres de décision.\n"
    "  • 'quickchart' pour des DONNÉES chiffrées : barres, courbes, camemberts, "
    "comparaisons, évolutions dans le temps.\n"
    "Puis génère le contenu :\n"
    "  • mermaid → un code Mermaid VALIDE (ex: 'flowchart TD\\n  A[X]-->B[Y]').\n"
    "  • quickchart → une config Chart.js (type + data.labels + data.datasets).\n"
    "Garde des libellés COURTS, lisibles à l'écran.\n"
    'Réponds UNIQUEMENT en JSON : {"tool":"mermaid|quickchart","mermaid":"<code ou null>",'
    '"chart":<config Chart.js ou null>,"titre":"titre court"}'
)

ANIM_SYSTEM = (
    "Tu génères des GRAPHIQUES ANIMÉS pour des vidéos YouTube tech/dev. À partir de la demande, "
    "choisis le meilleur template et fournis ses données :\n"
    "  • 'BarChart' — comparaison, classement, parts de marché. "
    'props: {"title": "...", "unit": "%", "items": [{"label": "...", "value": <nombre>}, …]}\n'
    "  • 'LineChart' — évolution dans le temps. "
    'props: {"title": "...", "unit": "", "color": "#3b82f6", "labels": ["2021", …], "data": [<nombres>]}\n'
    "  • 'StatCard' — UN chiffre clé marquant. "
    'props: {"value": <nombre>, "prefix": "", "suffix": "%", "label": "...", "sublabel": "...", "color": "#22c55e", "decimals": 0}\n'
    "  • 'Pie' — répartition / parts en cercle (donut). "
    'props: {"title": "...", "items": [{"label": "...", "value": <nombre>}, …]}\n'
    "  • 'Comparison' — A vs B (face-à-face de deux options). "
    'props: {"title": "...", "unit": "%", "left": {"label": "...", "value": <n>, "color": "#10a37f"}, "right": {"label": "...", "value": <n>, "color": "#d97706"}}\n'
    "  • 'BarRace' — classement (barres horizontales triées). "
    'props: {"title": "...", "unit": "", "items": [{"label": "...", "value": <nombre>}, …]}\n'
    "  • 'Timeline' — chronologie d'événements datés. "
    'props: {"title": "...", "color": "#3b82f6", "events": [{"date": "2022", "label": "..."}, …]}\n'
    "  • 'Layers' — couches / pile / CHAÎNE DE VALEUR : maillons empilés apparaissant un par un. "
    'props: {"title": "...", "layers": [{"label": "...", "sublabel": "(optionnel)"}, …]}  '
    "(ex : chaîne de valeur IA → Applicatif, Modèle de fondation, Compute, Silicium)\n"
    "N'invente AUCUN chiffre absent de la demande : si les données manquent, déduis-les seulement "
    "si la demande les contient explicitement, sinon choisis StatCard avec le chiffre fourni.\n"
    "Libellés courts. Couleurs en hex.\n"
    "Propose 2 à 3 VARIANTES de présentation des MÊMES données, avec des templates DIFFÉRENTS "
    "quand c'est pertinent (ex : parts de marché → BarChart, Pie, BarRace). Si une seule présentation "
    "a du sens (ex : un chiffre unique), renvoie 1 variante.\n"
    'Réponds UNIQUEMENT en JSON : {"variants": [{"template": "BarChart|LineChart|StatCard|Pie|Comparison|BarRace|Timeline|Layers", "props": {…}, "name": "slug-court"}, …]}'
)

THUMBNAIL_SYSTEM = (
    "Tu es directeur artistique des miniatures YouTube d'une chaîne tech/podcast.\n"
    "À partir du SUJET/SCRIPT fourni, génère le nombre demandé d'idées de miniatures VARIÉES "
    "(angles différents : provocateur, curieux, éducatif, dramatique, contre-intuitif…).\n"
    "Pour chaque idée :\n"
    "  • titre : court et accrocheur en FRANÇAIS (style YouTube percutant, sans clickbait mensonger)\n"
    "  • prompt : description en ANGLAIS pour générer l'image — composition forte, sujet visuel clair, "
    "émotion/expression marquée, contraste élevé, couleurs vives, style miniature YouTube, espace pour du texte. "
    "PAS de texte dans l'image (le titre est ajouté séparément).\n"
    'Réponds UNIQUEMENT en JSON : {"ideas": [{"titre": "...", "prompt": "..."}]}'
)

DERUSH_SYSTEM = (
    "Tu es assistant de DÉRUSHAGE vidéo. On te donne la TRANSCRIPTION timecodée d'un rush où "
    "l'orateur RÉPÈTE souvent les mêmes phrases jusqu'à réussir, et le SCRIPT cible visé.\n"
    "Sélectionne, DANS L'ORDRE DU SCRIPT, les passages correspondant à la DERNIÈRE bonne prise "
    "de chaque partie (la version propre : sans hésitation, faux départ, bafouillage ni reprise).\n"
    "Ignore les ratés et répétitions intermédiaires. Couvre tout le script, dans l'ordre final de lecture.\n"
    "Donne les intervalles en SECONDES décimales à CONSERVER.\n"
    'Réponds UNIQUEMENT en JSON : {"segments":[{"start":<s>,"end":<s>,"texte":"..."}]}'
)

CUSTOM_ANIM_SYSTEM = (
    "Tu génères le CODE d'un composant Remotion (React) pour une animation vidéo SUR MESURE "
    "(chaîne YouTube tech). Contraintes STRICTES :\n"
    "• Exporte EXACTEMENT : export const Custom = ({bg}) => { ... }\n"
    "• Imports autorisés UNIQUEMENT : import {useCurrentFrame, useVideoConfig, spring, interpolate, "
    "AbsoluteFill, Sequence, Easing} from 'remotion'; (et rien d'autre)\n"
    "• AUCUN fetch, AUCUNE lib externe, AUCUN accès réseau/fichier/import d'image.\n"
    "• Format : 1920x1080, 30 fps, durée 180 frames (6 s). Anime via useCurrentFrame() + spring/interpolate.\n"
    "• Mets les DONNÉES en dur (déduites de la demande). Dessine les formes en SVG ou <div>.\n"
    "• Fond : backgroundColor: bg || '#0f172a' sur l'AbsoluteFill racine.\n"
    "• Style soigné : fontFamily 'Inter, sans-serif', gros titres, palette vive, ombres/glow, apparitions séquentielles.\n"
    "• Code COMPLET, VALIDE, prêt à compiler.\n"
    "Réponds UNIQUEMENT avec le code JSX brut. PAS de ``` , PAS de markdown, PAS d'explication."
)

ANIM_BATCH_SYSTEM = (
    "Tu es directeur artistique DATA pour une chaîne YouTube tech. On te donne un SCRIPT.\n"
    "Repère les moments contenant des DONNÉES (chiffres, comparaisons, évolutions, classements, "
    "répartitions, chronologies, chaînes/piles de valeur) qui gagneraient à être ANIMÉS.\n"
    "Pour chaque moment, choisis le template le plus adapté et fournis ses props :\n"
    "  BarChart{title,unit,items:[{label,value}]} · LineChart{title,unit,color,labels,data} · "
    "StatCard{value,suffix,label,sublabel,color} · Pie{title,items} · "
    "Comparison{title,unit,left:{label,value},right:{label,value}} · BarRace{title,unit,items} · "
    "Timeline{title,color,events:[{date,label}]} · Layers{title,layers:[{label,sublabel}]}\n"
    "N'invente AUCUN chiffre absent du script. Ne retiens que les moments réellement pertinents.\n"
    'Réponds UNIQUEMENT en JSON : {"items": [{"extrait": "...", "template": "...", "props": {…}, "name": "slug"}]}'
)

GRAPH_SCAN_SYSTEM = (
    "Tu es directeur artistique DATA pour une chaîne tech/dev.\n"
    "On te donne un script. Repère les moments qui gagneraient à être illustrés par un "
    "GRAPHIQUE de données (chiffres, évolutions, comparaisons, parts de marché) ou un "
    "SCHÉMA (flux, architecture, processus, timeline, organigramme).\n"
    "Pour chaque moment, fournis une spec directement exploitable :\n"
    "  • 'quickchart' → config Chart.js (type + data.labels + data.datasets)\n"
    "  • 'mermaid' → code Mermaid valide (libellés COURTS)\n"
    "N'invente PAS de chiffres précis absents du script : si les données manquent, fais un "
    "SCHÉMA mermaid plutôt qu'un graphe chiffré. Ne propose que des moments réellement pertinents.\n"
    'Réponds UNIQUEMENT en JSON : {"items":[{"extrait":"...","tool":"mermaid|quickchart",'
    '"mermaid":"<ou null>","chart":<ou null>,"titre":"court"}]}'
)

# ---- Sessions : persistées dans /data pour survivre aux redémarrages du conteneur ----
# {str(chat_id): {segments, idx, counter, props:{id:{...}}, kept:[id], waiting_script, diagram}}
SESSIONS_FILE = os.path.join(DATA_DIR, "broll_sessions.json")


def _fix_props(p):
    if isinstance(p, dict) and isinstance(p.get("props"), dict):
        p["props"] = {int(k): v for k, v in p["props"].items()}


def load_sessions():
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
        # JSON transforme les clés de dict en str → on restaure les IDs (props) en int,
        # dans chaque projet (nouveau format) ou à plat (ancien format, migré ensuite).
        for st in data.values():
            if isinstance(st, dict) and isinstance(st.get("projects"), dict):
                for proj in st["projects"].values():
                    _fix_props(proj)
            else:
                _fix_props(st)
        return data
    except Exception:
        return {}


def save_sessions():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(SESSIONS, f, ensure_ascii=False)
        os.replace(tmp, SESSIONS_FILE)  # écriture atomique
    except Exception as e:
        print(f"save_sessions erreur : {e}", file=sys.stderr)


SESSIONS = load_sessions()


def _new_project(name):
    """Un projet = un espace de travail isolé (IDs et segments relatifs), un dossier Caddy."""
    return {"name": name, "segments": [], "idx": 0, "counter": 0, "props": {},
            "kept": [], "script": "", "awaiting": None, "pending_script": "",
            "auto_anim": True, "diagram": None, "custom_code": "", "custom_desc": ""}


def chat_state(chat_id):
    """État d'un chat : {current: slug, projects: {slug: projet}}. Migre l'ancien format à plat."""
    cid = str(chat_id)
    st = SESSIONS.get(cid)
    if st is None:
        st = {"current": "projet-1", "projects": {"projet-1": _new_project("projet-1")}}
        SESSIONS[cid] = st
    elif "projects" not in st:                      # ancien format : st EST un projet
        for k, v in _new_project("projet-1").items():
            st.setdefault(k, v)
        SESSIONS[cid] = {"current": "projet-1", "projects": {"projet-1": st}}
        st = SESSIONS[cid]
    return st


def project_slug(chat_id):
    return chat_state(chat_id)["current"]


def session(chat_id):
    """Renvoie le PROJET courant (même forme qu'avant : segments, idx, counter, props, kept…)."""
    st = chat_state(chat_id)
    if st["current"] not in st["projects"]:
        st["current"] = next(iter(st["projects"]), "projet-1")
        st["projects"].setdefault(st["current"], _new_project(st["current"]))
    proj = st["projects"][st["current"]]
    for k, v in _new_project(proj.get("name", "projet")).items():
        proj.setdefault(k, v)
    return proj


def new_id(s):
    s["counter"] += 1
    return s["counter"]


# ---- Telegram ----
def tg(method, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=90)
    r.raise_for_status()
    return r.json().get("result")


def send(chat_id, text, html=False, preview=False):
    params = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": not preview}
    if html:
        params["parse_mode"] = "HTML"
    try:
        tg("sendMessage", **params)
    except Exception as e:
        print(f"sendMessage erreur : {e}", file=sys.stderr)


# ---- AI ----
def _chat(system, user, temperature=0.3, model=None, think=False):
    if not CHUTES_KEY:
        return None
    payload = {"model": model or CHUTES_MODEL,
               "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
               "temperature": temperature}
    # DECISION: thinking OFF par défaut → réponse directe (~2s, évite content=null/timeout).
    # think=True pour les tâches de raisonnement ponctuelles (ex: sélection des prises au dérush).
    if not think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        r = requests.post(f"{CHUTES_BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {CHUTES_KEY}"},
                          json=payload, timeout=180)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
    except Exception as e:
        print(f"_chat erreur : {e}", file=sys.stderr)
        return None
    out = msg.get("content") or msg.get("reasoning_content") or ""
    if "</think>" in out:
        out = out.rsplit("</think>", 1)[1]
    return out.strip() or None


def _chat_json(system, user, think=False):
    out = _chat(system, user, think=think)
    if not out:
        return None
    try:
        return json.loads(out[out.find("{"):out.rfind("}") + 1])
    except Exception as e:
        print(f"JSON parse erreur : {e}", file=sys.stderr)
        return None


def segment_script(script, n=None, avoid=None):
    """Segmente le script. n = nombre exact voulu (sinon 8-12). avoid = extraits déjà traités."""
    extra = ""
    if n:
        extra += f"\n\nIMPÉRATIF : identifie EXACTEMENT {n} moments (ni plus, ni moins)."
    if avoid:
        extra += ("\n\nÉVITE ces moments DÉJÀ traités (ne propose rien de similaire) :\n"
                  + " | ".join(a for a in avoid[:50] if a))
    d = _chat_json(SEGMENT_SYSTEM, f"SCRIPT :\n{script}{extra}")
    return d.get("segments") if d else None


def scan_graph_moments(script, n=None):
    """Repère les moments du script à illustrer par graphique/schéma. Renvoie liste d'items."""
    extra = f"\n\nLimite-toi aux {n} moments les plus pertinents." if n else ""
    d = _chat_json(GRAPH_SCAN_SYSTEM, f"SCRIPT :\n{script}{extra}")
    return d.get("items") if d else None


# ---- yt-dlp ----
def _seconds_to_hms(s):
    s = int(s or 0)
    h, m = divmod(s, 3600)
    m, sec = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def yt_info(url):
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "socket_timeout": 20, "extract_flat": False}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"yt-dlp erreur ({url}) : {e}", file=sys.stderr)
        return None


def select_timestamp(url, visuel):
    """Extrait chapitres/description et laisse l'AI choisir le meilleur moment."""
    info = yt_info(url)
    if not info:
        return None
    chapters_text = ""
    if info.get("chapters"):
        chapters_text = "\nCHAPITRES :\n" + "\n".join(
            f"  {int(c['start_time'])}s→{int(c['end_time'])}s : {c['title']}"
            for c in info["chapters"])
    user_content = (
        f"VIDÉO : {info.get('title', '')}\n"
        f"DURÉE : {int(info.get('duration') or 0)}s\n"
        f"DESCRIPTION : {(info.get('description') or '')[:600]}"
        f"{chapters_text}\n\nB-ROLL À ILLUSTRER : {visuel}")
    d = _chat_json(TIMESTAMP_SYSTEM, user_content)
    if not d or not d.get("pertinent", True):
        return None
    start = int(d.get("start") or 0)
    end = int(d.get("end") or min(start + 20, int(info.get("duration") or 9999)))
    url_ts = f"{url}&t={start}" if "?" in url else f"{url}?t={start}"
    return {"title": info.get("title", ""), "start": start, "end": end,
            "hms_start": _seconds_to_hms(start), "hms_end": _seconds_to_hms(end),
            "raison": d.get("raison", ""), "url_ts": url_ts}


# ---- SearXNG + filtrage technique ----
def searxng(query, category="images", limit=12):
    try:
        r = requests.get(f"{SEARXNG_URL}/search",
                         params={"q": query, "categories": category,
                                 "format": "json", "language": "en"},
                         timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])[:limit]
    except Exception as e:
        print(f"SearXNG erreur ({category} / {query}) : {e}", file=sys.stderr)
        return []


def _img_width(resolution):
    nums = re.findall(r"\d+", str(resolution or ""))
    return int(nums[0]) if nums else None


def is_watermarked(img):
    """Détecte un filigrane par la source : banque d'images payante ou motif d'URL d'aperçu."""
    src = (img.get("img_src") or "").lower()
    page = (img.get("url") or "").lower()
    blob = src + " " + page
    if any(d in blob for d in WATERMARK_DOMAINS):
        return True
    if any(p in src for p in WATERMARK_PATTERNS):
        return True
    return False


def filter_images(results, min_width=MIN_WIDTH):
    """Rejette SVG, icônes, domaines poubelle, filigranes et basse résolution. Trie par taille."""
    kept = []
    for r in results:
        src = (r.get("img_src") or r.get("url") or "").lower()
        fmt = (r.get("img_format") or "").upper()
        eng = (r.get("engine") or "").lower()
        if "svg" in fmt or src.endswith(".svg"):
            continue
        if any(ie in eng for ie in ICON_ENGINES):
            continue
        if any(jd in src for jd in JUNK_DOMAINS):
            continue
        if is_watermarked(r):
            continue
        w = _img_width(r.get("resolution"))
        if w is not None and w < min_width:
            continue
        r["_w"] = w or 0
        kept.append(r)
    kept.sort(key=lambda x: (x.get("_w", 0), x.get("score", 0)), reverse=True)
    return kept


# ---- Persistance de la sélection ----
def persist_kept(chat_id, s):
    """Écrit la sélection conservée dans le dossier Caddy du projet : /exports/<projet>/recap.json."""
    try:
        d = os.path.join(EXPORT_DIR, project_slug(chat_id))
        os.makedirs(d, exist_ok=True)
        kept_full = [s["props"][i] for i in s["kept"] if i in s["props"]]
        with open(os.path.join(d, "recap.json"), "w") as f:
            json.dump(kept_full, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"persist_kept erreur : {e}", file=sys.stderr)


def tg_send_document(chat_id, filename, data, caption=""):
    """Envoie un fichier (multipart) dans Telegram."""
    try:
        requests.post(f"{TG_API}/sendDocument",
                      data={"chat_id": chat_id, "caption": caption[:1024]},
                      files={"document": (filename, data)}, timeout=180)
    except Exception as e:
        print(f"sendDocument erreur : {e}", file=sys.stderr)


def _slug(text, maxlen=40):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen].strip("-") or "broll"


def _basename(k):
    """Nom structuré : <chunk>_<id>_<description courte>."""
    seg = str(k.get("segment", "x")).zfill(2)
    pid = str(k.get("id", "x")).zfill(2)
    return f"{seg}_{pid}_{_slug(k.get('title'))}"


def _img_ext(resp, url):
    ctype = resp.headers.get("content-type", "").split(";")[0]
    ext = mimetypes.guess_extension(ctype) or os.path.splitext(urlparse(url).path)[1] or ".jpg"
    return ".jpg" if ext in (".jpe", ".jpeg") else ext


def download_video(url, dest, basename):
    """Télécharge une vidéo YouTube (≤720p, mp4) via yt-dlp. Renvoie le nom du fichier."""
    import yt_dlp
    opts = {"format": "bv*[height<=720]+ba/b[height<=720]/best",
            "outtmpl": os.path.join(dest, basename + ".%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True, "socket_timeout": 30}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return os.path.basename(ydl.prepare_filename(info))


def _render_diagram_bytes(prop):
    """Re-render un diagramme conservé (prop type diagram) en bytes + extension."""
    if prop.get("tool") == "quickchart":
        return render_quickchart(prop["spec"]), "png"
    return render_mermaid(prop["spec"]), "jpg"


def export_selection(chat_id, s):
    """Export UNIFIÉ : images HD + diagrammes + videos.txt + ZIP, servis par Caddy (tunnel).
    Le zip part aussi dans Telegram s'il est léger. JAMAIS de téléchargement vidéo serveur
    (YouTube bloque les IP datacenter) : les vidéos sont fournies en liens dans videos.txt."""
    kept = [s["props"][i] for i in s["kept"] if i in s["props"]]
    if not kept:
        send(chat_id, "Rien à exporter. Conserve d'abord avec /garde <id>.")
        return
    images = [k for k in kept if k.get("type") == "image"]
    videos = [k for k in kept if k.get("type") == "video"]
    diagrams = [k for k in kept if k.get("type") == "diagram"]
    slug = project_slug(chat_id)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(EXPORT_DIR, slug, f"export-{ts}")
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        send(chat_id, f"⚠️ Impossible de créer le dossier d'export : {e}")
        return
    send(chat_id, f"📦 Export : {len(images)} image(s), {len(diagrams)} diagramme(s), "
                  f"{len(videos)} vidéo(s) en liens…")
    recap = ["B-ROLLS CONSERVÉS", ""]
    if images:
        recap.append("== IMAGES ==")
    for k in images:
        url = k.get("url", "")
        try:
            resp = requests.get(url, headers=_UA, timeout=30)
            resp.raise_for_status()
            name = _basename(k) + _img_ext(resp, url)
            with open(os.path.join(dest, name), "wb") as f:
                f.write(resp.content)
            recap.append(f"{name}  ·  {url}")
        except Exception as e:
            recap.append(f"(échec image)  {url}  — {e}")
    if diagrams:
        recap += ["", "== DIAGRAMMES =="]
    for k in diagrams:
        try:
            data, ext = _render_diagram_bytes(k)
            name = _basename(k) + "." + ext
            with open(os.path.join(dest, name), "wb") as f:
                f.write(data)
            recap.append(f"{name}  ·  {k.get('title', '')}")
        except Exception as e:
            recap.append(f"(échec diagramme)  {k.get('title', '')}  — {e}")
    anims = [k for k in kept if k.get("type") == "anim"]
    if anims:
        recap += ["", "== ANIMATIONS (Remotion) =="]
    for k in anims:
        srcdir = os.path.join(EXPORT_DIR, k.get("dir", ""))
        files = os.listdir(srcdir) if os.path.isdir(srcdir) else []
        for fn in files:
            if fn.endswith(".mp4"):  # MP4 léger → copié dans l'export
                try:
                    shutil.copy(os.path.join(srcdir, fn), os.path.join(dest, f"{_basename(k)}.mp4"))
                    recap.append(f"{_basename(k)}.mp4")
                except Exception as e:
                    recap.append(f"(échec anim) {fn} — {e}")
            else:  # .mov ProRes lourd → laissé dans la galerie, référencé par lien
                recap.append(f"{fn} (transparent, télécharger : {k.get('url')}{fn})")

    if videos:
        recap += ["", "== VIDÉOS YOUTUBE (à télécharger en local — cf. videos.txt) =="]
        with open(os.path.join(dest, "videos.txt"), "w") as f:
            for k in videos:
                f.write(f"# {_basename(k)}\n{k.get('url', '')}\n\n")
                recap.append(f"{_basename(k)}  ·  {k.get('url', '')}")
    with open(os.path.join(dest, "recap.txt"), "w") as f:
        f.write("\n".join(recap))

    # ZIP systématique de tout le dossier
    zip_path = os.path.join(dest, "broll_export.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in sorted(os.listdir(dest)):
            if fn != "broll_export.zip":
                zf.write(os.path.join(dest, fn), fn)
    zip_size = os.path.getsize(zip_path)

    base = f"{EXPORT_BASE_URL}/{slug}/export-{ts}"
    send(chat_id, f"✅ Export prêt (projet « {slug} », via tunnel SSH -L 8088:localhost:8088) :\n"
                  f"📁 dossier : {base}/\n📦 zip : {base}/broll_export.zip")
    if zip_size <= 45 * 1024 * 1024:
        with open(zip_path, "rb") as f:
            tg_send_document(chat_id, "broll_export.zip", f.read(),
                             caption=f"📦 {len(images)} img + {len(diagrams)} diag · "
                                     f"vidéos en liens (videos.txt)")
    else:
        send(chat_id, f"(zip {zip_size // 1024 // 1024} Mo : trop lourd pour Telegram, "
                      "récupère-le via le lien)")


# ---- Diagrammes (l'IA choisit mermaid ou quickchart) ----
_UA = {"User-Agent": "Mozilla/5.0"}


def generate_diagram(description, current=None):
    """Demande à l'IA un diagramme (nouveau ou modifié). Renvoie {tool, spec, titre} ou None."""
    if current:
        if current["tool"] == "quickchart":
            cur_txt = "Config Chart.js actuelle :\n" + json.dumps(current["spec"], ensure_ascii=False)
        else:
            cur_txt = "Code Mermaid actuel :\n" + str(current["spec"])
        user = (f"OUTIL ACTUEL : {current['tool']}\n{cur_txt}\n\n"
                f"MODIFICATION DEMANDÉE : {description}\n"
                "Renvoie le diagramme COMPLET mis à jour (change d'outil si pertinent).")
    else:
        user = f"DEMANDE : {description}"
    d = _chat_json(DIAGRAM_SYSTEM, user)
    if not d or "tool" not in d:
        return None
    tool = "quickchart" if d.get("tool") == "quickchart" else "mermaid"
    spec = d.get("chart") if tool == "quickchart" else (d.get("mermaid") or d.get("code"))
    if not spec:
        return None
    return {"tool": tool, "spec": spec, "titre": d.get("titre", "")}


def render_mermaid(code):
    # Thème dark (texte clair) + fond TRANSPARENT (PNG alpha) pour incrustation vidéo.
    if "%%{init" not in code:
        code = '%%{init: {"theme":"dark"}}%%\n' + code
    b64 = base64.urlsafe_b64encode(code.encode()).decode()
    r = requests.get(f"https://mermaid.ink/img/{b64}?type=png", headers=_UA, timeout=30)
    r.raise_for_status()
    return r.content


# Palette moderne + texte clair (visible sur incrustation/fond sombre)
QC_PALETTE = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7",
              "#06b6d4", "#ec4899", "#84cc16"]
QC_TEXT = "#f1f5f9"


def _style_chart(chart):
    """Injecte un style moderne : palette, texte clair, GROS titre, AUCUN quadrillage."""
    chart = dict(chart)
    ctype = chart.get("type", "bar")
    data = chart.setdefault("data", {})
    labels = data.get("labels", [])
    for i, ds in enumerate(data.get("datasets", [])):
        color = QC_PALETTE[i % len(QC_PALETTE)]
        if ctype in ("line", "radar"):
            ds.setdefault("borderColor", color)
            ds.setdefault("backgroundColor", color + "33")
            ds.setdefault("borderWidth", 3)
            ds.setdefault("tension", 0.35)
            ds.setdefault("pointRadius", 4)
            ds.setdefault("pointBackgroundColor", color)
        elif ctype in ("pie", "doughnut", "polarArea"):
            n = len(labels) or len(ds.get("data", []))
            ds.setdefault("backgroundColor", [QC_PALETTE[j % len(QC_PALETTE)] for j in range(n)])
            ds.setdefault("borderColor", "#0f172a")
            ds.setdefault("borderWidth", 2)
        else:  # bar
            ds.setdefault("backgroundColor", color)
            ds.setdefault("borderRadius", 6)
            ds.setdefault("borderWidth", 0)
    opts = chart.setdefault("options", {})
    plugins = opts.setdefault("plugins", {})
    plugins.setdefault("legend", {}).setdefault("labels", {})["color"] = QC_TEXT
    title = plugins.setdefault("title", {})
    title.setdefault("color", QC_TEXT)
    title.setdefault("font", {"size": 26, "weight": "bold"})
    # Pas de quadrillage + ticks clairs
    scales = opts.setdefault("scales", {})
    for ax in ("x", "y", "r"):
        a = scales.setdefault(ax, {})
        a.setdefault("ticks", {})["color"] = QC_TEXT
        a["grid"] = {"display": False, "drawBorder": False}
    return chart


def render_quickchart(chart):
    # HD ×2 + fond TRANSPARENT, style moderne sans quadrillage.
    payload = {"chart": _style_chart(chart), "width": 1280, "height": 720,
               "devicePixelRatio": 2, "backgroundColor": "transparent", "format": "png"}
    r = requests.post("https://quickchart.io/chart", json=payload, timeout=30)
    r.raise_for_status()
    return r.content


def tg_send_photo_bytes(chat_id, data, caption="", filename="diagram.png"):
    try:
        requests.post(f"{TG_API}/sendPhoto",
                      data={"chat_id": chat_id, "caption": caption[:1024]},
                      files={"photo": (filename, data)}, timeout=60)
    except Exception as e:
        print(f"sendPhoto erreur : {e}", file=sys.stderr)


def handle_diagram(chat_id, s, desc, is_modif):
    if not desc:
        send(chat_id, "Usage : /schema <description>  ·  /modif <instruction>")
        return
    current = s.get("diagram")
    if is_modif and not current:
        send(chat_id, "Aucun diagramme en cours. Fais /schema <description> d'abord.")
        return
    send(chat_id, "✏️ Modification du diagramme…" if is_modif else "🎨 Génération du diagramme…")
    d = generate_diagram(desc, current=current if is_modif else None)
    if not d:
        send(chat_id, "❌ Échec de génération. Reformule.")
        return
    try:
        img = render_quickchart(d["spec"]) if d["tool"] == "quickchart" else render_mermaid(d["spec"])
    except Exception as e:
        send(chat_id, f"⚠️ Échec du rendu ({d['tool']}) : {e}")
        return
    ext = "png" if d["tool"] == "quickchart" else "jpg"
    pid = new_id(s)
    s["props"][pid] = {"id": pid, "type": "diagram", "tool": d["tool"],
                       "spec": d["spec"], "title": d.get("titre", ""), "segment": 0}
    tg_send_photo_bytes(chat_id, img, filename=f"diagram.{ext}",
                        caption=f"🆔 #{pid} · 🧩 {d.get('titre', '')} · {d['tool']}\n"
                                f"/garde {pid} · ✏️ /modif <instruction> pour ajuster")
    s["diagram"] = d


def render_remotion(template, props, name, fmt="both", project=""):
    """Appelle le service Remotion. Renvoie le dict {ok, dir, files, url} ou lève.
    project = slug du projet → le service écrit dans /exports/<project>/anim/<name>/."""
    r = requests.post(f"{REMOTION_URL}/render",
                      json={"template": template, "props": props, "format": fmt,
                            "name": name, "project": project},
                      timeout=300)
    r.raise_for_status()
    return r.json()


def render_custom(code, name, project="", fmt="mp4"):
    """Envoie un composant Remotion (code généré par le LLM) au service pour rendu."""
    r = requests.post(f"{REMOTION_URL}/render_custom",
                      json={"code": code, "name": name, "project": project, "format": fmt},
                      timeout=300)
    r.raise_for_status()
    return r.json()


def _strip_code_fences(t):
    t = (t or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")[1:]                       # retire ```jsx
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip()


def _custom_render_and_send(chat_id, s, code, desc):
    """Rend un code Custom (1 auto-correction), l'envoie, et MÉMORISE le code réussi (pour /modcustom)."""
    name = f"custom{int(time.time())}"
    slug = project_slug(chat_id)
    try:
        res = render_custom(code, name, slug, "mp4")
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    if not res.get("ok"):                                # auto-correction : on renvoie l'erreur au LLM
        send(chat_id, "⚙️ Erreur de rendu — correction automatique…")
        fix = _chat(CUSTOM_ANIM_SYSTEM,
                    f"{desc}\n\nLe code suivant a ÉCHOUÉ :\n{code}\n\nERREUR :\n{str(res.get('error', ''))[:400]}\n"
                    "Corrige et renvoie UNIQUEMENT le code complet corrigé.")
        code2 = _strip_code_fences(fix)
        if code2:
            code = code2
            try:
                res = render_custom(code, name, slug, "mp4")
            except Exception as e:
                res = {"ok": False, "error": str(e)}
    if not res.get("ok"):
        send(chat_id, f"❌ Rendu sur mesure échoué : {str(res.get('error'))[:300]}")
        return
    s["custom_code"] = code                              # mémorisé pour la modification ultérieure
    s["custom_desc"] = desc
    _send_anim_result(chat_id, s, "Custom", {"title": desc[:60]}, res, "🛠️ sur mesure")
    send(chat_id, "✏️ Pour ajuster sans repartir de zéro : /modcustom <ce qu'il faut changer>")


def handle_custom(chat_id, s, desc):
    """Animation SUR MESURE : le LLM génère le code Remotion, rendu à la volée."""
    if not desc:
        send(chat_id, "Usage : /custom <description précise de l'animation voulue>")
        return
    if not REMOTION_URL:
        send(chat_id, "Service d'animation non configuré.")
        return
    send(chat_id, "🛠️ Génération du code de l'animation + rendu (~1 min)…")
    code = _strip_code_fences(_chat(CUSTOM_ANIM_SYSTEM, desc))
    if not code:
        send(chat_id, "❌ Génération du code impossible. Reformule.")
        return
    _custom_render_and_send(chat_id, s, code, desc)


def handle_custom_modif(chat_id, s, instruction):
    """Modifie l'animation sur mesure courante SANS repartir de zéro."""
    cur = s.get("custom_code")
    if not cur:
        send(chat_id, "Aucune animation sur mesure en cours. Fais d'abord /custom <description>.")
        return
    if not instruction:
        send(chat_id, "Usage : /modcustom <ce qu'il faut changer>  (ex: /modcustom fond blanc, plus lent, ajoute un 4e maillon)")
        return
    send(chat_id, "🛠️ Modification du code + rendu…")
    user = (f"CODE ACTUEL :\n{cur}\n\nMODIFICATION DEMANDÉE :\n{instruction}\n"
            "Renvoie UNIQUEMENT le code COMPLET modifié (mêmes contraintes).")
    code = _strip_code_fences(_chat(CUSTOM_ANIM_SYSTEM, user))
    if not code:
        send(chat_id, "❌ Modification impossible. Reformule.")
        return
    desc = (s.get("custom_desc", "") + " | modif: " + instruction)[:200]
    _custom_render_and_send(chat_id, s, code, desc)


def tg_send_video(chat_id, data, caption=""):
    try:
        requests.post(f"{TG_API}/sendVideo",
                      data={"chat_id": chat_id, "caption": caption[:1024], "supports_streaming": "true"},
                      files={"video": ("anim.mp4", data)}, timeout=180)
    except Exception as e:
        print(f"sendVideo erreur : {e}", file=sys.stderr)


def _send_anim_result(chat_id, s, template, props, res, label):
    """Envoie un rendu d'animation dans Telegram (vidéo + document MP4) et enregistre le prop.
    Le MP4 est lu DIRECTEMENT sur le volume /exports (localhost:8088 n'est pas joignable d'ici)."""
    url = res["url"]
    rel_dir = res.get("dir", "")
    mp4 = next((f for f in res.get("files", []) if f.endswith(".mp4")), None)
    name = rel_dir.rsplit("/", 1)[-1] or template
    pid = new_id(s)
    s["props"][pid] = {"id": pid, "type": "anim", "template": template, "props": props,
                       "title": props.get("title") or props.get("label") or template,
                       "url": url, "dir": rel_dir, "segment": 0}
    cap = f"🆔 #{pid} · {label} · 🎬 {template}\n📁 {url}\n/garde {pid}"
    sent = False
    if mp4:
        local = os.path.join(EXPORT_DIR, rel_dir, mp4)
        try:
            with open(local, "rb") as f:
                data = f.read()
            tg_send_video(chat_id, data, caption=cap)                       # preview jouable
            tg_send_document(chat_id, f"{name}.mp4", data, caption="📎 MP4 (fichier propre)")
            sent = True
        except Exception as e:
            print(f"_send_anim_result envoi échoué ({local}) : {e}", file=sys.stderr)
    if not sent:
        send(chat_id, cap)   # au minimum, le lien galerie


def handle_anim(chat_id, s, desc):
    """Génère 2-3 VARIANTES animées (Remotion), envoyées dans Telegram en MP4 (vidéo + fichier)."""
    if not desc:
        send(chat_id, "Usage : /anim <description>  (ex: /anim parts de marché : OpenAI 38%, Anthropic 22%, Google 18%)")
        return
    spec = _chat_json(ANIM_SYSTEM, desc)
    variants = spec.get("variants") if spec else None
    if not variants and spec and spec.get("template"):     # repli ancien format mono
        variants = [spec]
    if not variants:
        send(chat_id, "❌ Je n'ai pas pu déterminer le graphique. Reformule avec des chiffres clairs.")
        return
    variants = [v for v in variants if v.get("template")][:3]
    send(chat_id, f"🎬 Génération de {len(variants)} variante(s)… (~{len(variants) * 25}s)")
    ok = 0
    for i, v in enumerate(variants, 1):
        template = v["template"]
        props = v.get("props", {})
        name = (v.get("name") or f"anim{int(time.time())}_{i}").replace("/", "_")
        try:
            res = render_remotion(template, props, name, "mp4", project_slug(chat_id))   # MP4 seul → rapide
        except Exception as e:
            send(chat_id, f"⚠️ Variante {i} : rendu indisponible ({e})")
            continue
        if not res.get("ok"):
            send(chat_id, f"⚠️ Variante {i} : {res.get('error')}")
            continue
        _send_anim_result(chat_id, s, template, props, res, f"variante {i}/{len(variants)}")
        ok += 1
    send(chat_id, f"✅ {ok} variante(s) envoyée(s). /garde <id> pour conserver."
                  if ok else "❌ Aucune variante n'a pu être rendue.")


def maybe_propose_anim(chat_id, s, seg, seg_n):
    """Dans le flux normal : si le segment contient des chiffres, propose une animation."""
    if not s.get("auto_anim", True) or not REMOTION_URL:
        return
    blob = f"{seg.get('extrait', '')} {seg.get('visuel', '')}"
    if not re.search(r"\d", blob):       # pas de chiffre → pas d'animation
        return
    spec = _chat_json(ANIM_SYSTEM, f"{seg.get('extrait', '')} — {seg.get('visuel', '')}")
    variants = (spec or {}).get("variants") or ([spec] if spec and spec.get("template") else [])
    variants = [v for v in variants if v.get("template")]
    if not variants:
        return
    v = variants[0]
    template, props = v["template"], v.get("props", {})
    name = (v.get("name") or f"seg{seg_n}_{int(time.time())}").replace("/", "_")
    send(chat_id, "📊 Ce moment se prête à une animation — rendu en cours…")
    try:
        res = render_remotion(template, props, name, "mp4", project_slug(chat_id))
        if res.get("ok"):
            _send_anim_result(chat_id, s, template, props, res, "💡 animation suggérée")
    except Exception as e:
        print(f"maybe_propose_anim erreur : {e}", file=sys.stderr)


def handle_anims(chat_id, s, n=None):
    """Mode batch : scanne le script et génère une animation par moment chiffré."""
    script = s.get("script", "")
    if not script:
        send(chat_id, "Aucun script en mémoire. Envoie d'abord un script.")
        return
    send(chat_id, "🎬 Repérage des moments à animer dans le script…")
    extra = f"\n\nLimite-toi aux {n} moments les plus forts." if n else ""
    done = [p.get("title", "") for p in s["props"].values() if p.get("type") == "anim"]
    if done:
        extra += "\n\nÉVITE ces moments DÉJÀ animés (n'en propose pas de similaires) : " + \
                 " | ".join(t for t in done if t)
    d = _chat_json(ANIM_BATCH_SYSTEM, f"SCRIPT :\n{script}{extra}")
    items = d.get("items") if d else None
    items = [it for it in (items or []) if it.get("template")][:8]
    if not items:
        send(chat_id, "Aucun moment chiffré pertinent trouvé (ou échec). Réessaie.")
        return
    send(chat_id, f"✅ {len(items)} animation(s) à rendre (~{len(items) * 25}s)…")
    ok = 0
    for i, it in enumerate(items, 1):
        template = it["template"]
        props = it.get("props", {})
        name = (it.get("name") or f"batch{int(time.time())}_{i}").replace("/", "_")
        try:
            res = render_remotion(template, props, name, "mp4", project_slug(chat_id))
        except Exception as e:
            send(chat_id, f"⚠️ Animation {i} : {e}")
            continue
        if not res.get("ok"):
            send(chat_id, f"⚠️ Animation {i} : {res.get('error')}")
            continue
        _send_anim_result(chat_id, s, template, props, res, f"« {it.get('extrait', '')[:40]} »")
        ok += 1
    send(chat_id, f"✅ {ok} animation(s) envoyée(s). /garde <id> pour conserver.")


def handle_graphiques(chat_id, s, n=None):
    """Scanne le script en mémoire et génère des diagrammes pour les moments-données."""
    script = s.get("script", "")
    if not script:
        send(chat_id, "Aucun script en mémoire. Envoie d'abord un script.")
        return
    send(chat_id, "📊 Repérage des moments à illustrer par un graphique/schéma…")
    items = scan_graph_moments(script, n)
    if not items:
        send(chat_id, "Aucun moment graphique pertinent trouvé (ou échec). Réessaie.")
        return
    sent = 0
    for it in items:
        tool = "quickchart" if it.get("tool") == "quickchart" else "mermaid"
        spec = it.get("chart") if tool == "quickchart" else (it.get("mermaid") or it.get("code"))
        if not spec:
            continue
        try:
            img = render_quickchart(spec) if tool == "quickchart" else render_mermaid(spec)
        except Exception as e:
            send(chat_id, f"⚠️ Rendu échoué ({tool}) : {e}")
            continue
        pid = new_id(s)
        s["props"][pid] = {"id": pid, "type": "diagram", "tool": tool,
                           "spec": spec, "title": it.get("titre", ""), "segment": 0}
        ext = "png" if tool == "quickchart" else "jpg"
        tg_send_photo_bytes(chat_id, img, filename=f"graph_{pid}.{ext}",
                            caption=f"🆔 #{pid} · 📊 {it.get('titre', '')}\n"
                                    f"« {it.get('extrait', '')[:60]} »\n/garde {pid}")
        sent += 1
    send(chat_id, f"✅ {sent} graphique(s) proposé(s). /garde <id> pour conserver, "
                  "/export pour tout récupérer.")


# ---- Envoi de propositions (avec ID unique) ----
def send_image_proposal(chat_id, s, img, seg_n):
    pid = new_id(s)
    url = img.get("img_src") or img.get("url")
    title = (img.get("title") or "")[:70]
    host = urlparse(img.get("url") or "").netloc
    w = img.get("_w") or "?"
    caption = f"🆔 #{pid} · 🖼 {title}\n📐 ~{w}px · 🔗 {host}\n/garde {pid}"
    s["props"][pid] = {"id": pid, "type": "image", "url": url,
                       "page": img.get("url", ""), "title": title, "segment": seg_n}
    try:
        tg("sendPhoto", chat_id=chat_id, photo=url, caption=caption[:1024])
    except Exception:
        send(chat_id, f'🆔 #{pid} · 🖼 <a href="{url}">{title}</a>', html=True)
    return pid


def send_video_proposal(chat_id, s, seg_n, url, title, ts=None):
    """Propose une vidéo, avec extrait suggéré (ts) si dispo, sinon le lien brut."""
    pid = new_id(s)
    host = urlparse(url).netloc
    if ts:
        store_url = ts["url_ts"]
        extrait = f"{ts['hms_start']}→{ts['hms_end']}"
        body = (f"🆔 #{pid} · 🎬 <b>{title[:70]}</b>\n"
                f"▶️ extrait suggéré : {ts['hms_start']} → {ts['hms_end']}\n"
                f"💡 {ts['raison']}\n🔗 {store_url}\n/garde {pid}")
    else:
        store_url = url
        extrait = ""
        body = (f"🆔 #{pid} · 🎬 <b>{title[:70]}</b>\n🌐 {host}\n🔗 {url}\n/garde {pid}")
    s["props"][pid] = {"id": pid, "type": "video", "url": store_url,
                       "title": title, "segment": seg_n, "extrait": extrait}
    send(chat_id, body, html=True, preview=True)
    return pid


# ---- Recherche d'un segment (réutilisé par le flux normal ET /cherche) ----
def _round_robin(pools):
    """Entrelace plusieurs listes pour diversifier les angles (1 de chaque, puis on tourne)."""
    out = []
    maxlen = max((len(p) for p in pools), default=0)
    for i in range(maxlen):
        for p in pools:
            if i < len(p):
                out.append(p[i])
    return out


def propose_images(chat_id, s, seg, queries, seg_n, total=3):
    """Cherche sur plusieurs angles (requêtes), filtre, diversifie. Renvoie True si au moins une."""
    if isinstance(queries, str):
        queries = [queries]
    queries = [q for q in queries if q][:3]
    seen, pools = set(), []
    for q in queries:
        pool = filter_images(searxng(q, "images", 10))
        uniq = []
        for r in pool:
            key = r.get("img_src") or r.get("url") or ""
            if key and key not in seen:
                seen.add(key)
                uniq.append(r)
        if uniq:
            pools.append(uniq)
    combined = _round_robin(pools)
    seg["img_pool"] = combined
    seg["img_shown"] = 0
    if not combined:
        send(chat_id, "⚠️ Aucune image exploitable (filtrée : icônes, filigranes, basse déf).")
        return False
    for img in combined[:total]:
        send_image_proposal(chat_id, s, img, seg_n)
    seg["img_shown"] = min(total, len(combined))
    return True


def propose_videos(chat_id, s, seg, query, seg_n, max_videos=3):
    """Propose plusieurs vidéos YouTube brutes (sans découpe d'extrait)."""
    vids = searxng(query, "videos", 12)
    yt_vids = [v for v in vids if "youtube.com" in v.get("url", "") or "youtu.be" in v.get("url", "")]
    seg["vid_pool"] = yt_vids
    seg["vid_shown"] = 0
    if not yt_vids:
        send(chat_id, "🎬 Aucune vidéo YouTube trouvée.")
        return False
    count = 0
    for v in yt_vids:
        if count >= max_videos:
            break
        url = v.get("url", "")
        title = v.get("title", "") or url
        if not url:
            continue
        send_video_proposal(chat_id, s, seg_n, url, title, ts=None)
        count += 1
    seg["vid_shown"] = count
    return count > 0


# ---- Traitement d'UN segment (step-by-step) ----
def process_segment(chat_id, s, seg):
    idx = s["idx"]
    total = len(s["segments"])
    stype = seg.get("type", "image")
    visuel = seg.get("visuel", "")
    extrait = seg.get("extrait", "")
    queries = seg.get("queries", [])
    icon = {"image": "🖼️", "video": "🎬", "ia": "🤖"}.get(stype, "•")

    qtext = " · ".join(queries) if queries else visuel
    send(chat_id,
         f"━━━━━━━━━━\n{icon} <b>Segment {idx + 1}/{total}</b>\n"
         f"<i>« {extrait} »</i>\n👁 {visuel}\n"
         f"🔎 recherche : {qtext}\n"
         f"<i>(ajuste avec /cherche tes mots)</i>",
         html=True)

    propose_images(chat_id, s, seg, queries or [visuel], idx + 1)
    propose_videos(chat_id, s, seg, queries[0] if queries else visuel, idx + 1)
    maybe_propose_anim(chat_id, s, seg, idx + 1)

    nxt = "/suivant pour la suite." if idx + 1 < total else "Dernier segment. /conserves pour le récap."
    send(chat_id, f"Flag : /garde <id> · autres images : /changer · {nxt}")


# ---- Miniatures YouTube (idéation : titres + images IA) ----
def pollinations_image(prompt, w=1280, h=720, seed=None):
    """Génère une image (text-to-image, Pollinations/FLUX, gratuit). Renvoie les bytes JPEG."""
    from urllib.parse import quote
    params = {"width": w, "height": h, "model": "flux", "nologo": "true"}
    if seed is not None:
        params["seed"] = seed
    r = requests.get(f"https://image.pollinations.ai/prompt/{quote(prompt)}", params=params, timeout=120)
    r.raise_for_status()
    return r.content


def handle_thumbnails(chat_id, s, arg, n=10):
    """À partir du script (ou d'un sujet donné), génère N idées de miniatures (titre + image IA)."""
    base = (arg or "").strip() or s.get("script", "")
    if not base:
        send(chat_id, "Donne un sujet : /miniatures <sujet>, ou envoie d'abord un script au projet.")
        return
    send(chat_id, f"🖼️ Génération de {n} idées de miniatures (titres + images, ~1 min)…")
    d = _chat_json(THUMBNAIL_SYSTEM, f"NOMBRE D'IDÉES : {n}\n\nSUJET / SCRIPT :\n{base[:4000]}")
    ideas = [i for i in (d or {}).get("ideas", []) if i.get("prompt")][:n]
    if not ideas:
        send(chat_id, "❌ Génération des idées impossible. Réessaie.")
        return
    slug = project_slug(chat_id)
    dest = os.path.join(EXPORT_DIR, slug, "thumbnails")
    os.makedirs(dest, exist_ok=True)
    ok = 0
    for i, idea in enumerate(ideas, 1):
        titre, prompt = idea.get("titre", ""), idea["prompt"]
        try:
            img = pollinations_image(prompt, 1280, 720, seed=i)
        except Exception as e:
            send(chat_id, f"⚠️ Miniature {i} échouée : {str(e)[:120]}")
            continue
        with open(os.path.join(dest, f"thumb_{i:02d}.jpg"), "wb") as f:
            f.write(img)
        try:
            tg_send_photo_bytes(chat_id, img, caption=f"#{i} · 💡 {titre}", filename=f"thumb_{i:02d}.jpg")
        except Exception:
            send(chat_id, f"#{i} · {titre}")
        ok += 1
    send(chat_id, f"✅ {ok} miniature(s) générée(s) (idéation).\n📁 {EXPORT_BASE_URL}/{slug}/thumbnails/")


# ---- Dérush (rushs → version débarrassée des ratés) ----
def list_rushs():
    try:
        files = [f for f in os.listdir(RUSHS_DIR) if f.lower().endswith(RUSH_EXTS)]
        return sorted(files, key=lambda f: os.path.getmtime(os.path.join(RUSHS_DIR, f)))
    except Exception:
        return []


def transcribe_rush(path):
    """Extrait l'audio (ffmpeg) et le transcrit via Whisper (JSON timecodé). Renvoie les segments."""
    audio = path + ".derush.wav"
    subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", audio],
                   check=True, capture_output=True)
    try:
        with open(audio, "rb") as f:
            r = requests.post(f"{WHISPER_URL}/asr",
                              params={"task": "transcribe", "language": WHISPER_LANG,
                                      "output": "json", "word_timestamps": "true", "encode": "true",
                                      # VAD : saute les silences (entre prises) → plus rapide + propre.
                                      "vad_filter": "true"},
                              files={"audio_file": (os.path.basename(audio), f, "audio/wav")},
                              timeout=5400)
        r.raise_for_status()
        return r.json().get("segments", [])
    finally:
        try:
            os.remove(audio)
        except OSError:
            pass


def derush_select(segments, script):
    """LLM : choisit les intervalles à conserver (dernières bonnes prises). Renvoie [{start,end,texte}]."""
    transcript = "\n".join(f"[{float(s.get('start', 0)):.1f}-{float(s.get('end', 0)):.1f}] "
                           f"{(s.get('text') or '').strip()}" for s in segments)
    user = (f"SCRIPT CIBLE :\n{script or '(non fourni — garde les dernières prises propres)'}\n\n"
            f"TRANSCRIPTION TIMECODÉE :\n{transcript}")
    d = _chat_json(DERUSH_SYSTEM, user, think=True)   # raisonnement ON → meilleure sélection des prises
    return d.get("segments") if d else None


def cut_concat(rush, segments, out, pre=0.3, post=0.4):
    """Découpe chaque passage dans un fichier temporaire (RAM minimale) puis concatène (demuxer).
    DECISION: évite le filter_complex tout-en-mémoire qui faisait OOM sur les longues sources."""
    tmpdir = tempfile.mkdtemp(prefix="derush_")
    parts = []
    try:
        for i, seg in enumerate(segments):
            s = max(0.0, float(seg["start"]) - pre)
            dur = max(0.1, float(seg["end"]) + post - s)
            part = os.path.join(tmpdir, f"p{i:04d}.mp4")
            # -ss avant -i = seek rapide ; réencode léger pour uniformiser (concat -c copy ensuite).
            subprocess.run(["ffmpeg", "-y", "-ss", f"{s}", "-i", rush, "-t", f"{dur}",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                            "-c:a", "aac", "-ar", "48000", "-avoid_negative_ts", "make_zero", part],
                           check=True, capture_output=True)
            parts.append(part)
        listf = os.path.join(tmpdir, "list.txt")
        with open(listf, "w") as f:
            for p in parts:
                f.write(f"file '{p}'\n")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
                        "-c", "copy", out], check=True, capture_output=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def handle_rushs(chat_id):
    rushs = list_rushs()
    if not rushs:
        send(chat_id, "Aucun rush. Dépose tes fichiers dans ~/rushs sur le serveur "
                      "(scp/sftp via le tunnel SSH), puis /rushs.")
        return
    lines = ["🎞️ Rushs disponibles :", ""]
    for r in rushs:
        mb = os.path.getsize(os.path.join(RUSHS_DIR, r)) // 1024 // 1024
        lines.append(f"• {r} ({mb} Mo)")
    lines += ["", "/derush <nom> pour dérusher (ou /derush seul = le plus récent)"]
    send(chat_id, "\n".join(lines))


def handle_derush(chat_id, s, arg):
    rushs = list_rushs()
    if not rushs:
        send(chat_id, "Aucun rush dans ~/rushs. Dépose un fichier (scp/sftp) puis réessaie.")
        return
    fname = None
    if arg:
        cand = [r for r in rushs if arg.lower() in r.lower()]
        fname = cand[0] if cand else None
        if not fname:
            send(chat_id, f"Rush « {arg} » introuvable. /rushs pour la liste.")
            return
    else:
        fname = rushs[-1]                       # le plus récemment déposé
    path = os.path.join(RUSHS_DIR, fname)

    # Pré-vérification : fichier lisible ? (un upload tronqué → moov manquant → message clair)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True)
    if probe.returncode != 0:
        err = probe.stderr.decode(errors="replace").strip()[:200]
        send(chat_id, f"⚠️ « {fname} » est illisible (upload incomplet ou corrompu).\n"
                      f"Détail : {err}\n\nPour un gros fichier, ré-uploade par rsync/scp "
                      "(le navigateur coupe souvent les transferts de plusieurs Go).")
        return

    send(chat_id, f"🎞️ Dérush de « {fname} » — transcription (plusieurs minutes selon la durée)…")
    try:
        segs = transcribe_rush(path)
    except Exception as e:
        send(chat_id, f"⚠️ Transcription échouée : {str(e)[:300]}")
        return
    if not segs:
        send(chat_id, "⚠️ Transcription vide.")
        return

    send(chat_id, f"📝 {len(segs)} segments transcrits — sélection des bonnes prises…")
    keep = derush_select(segs, s.get("script", ""))
    if not keep:
        send(chat_id, "⚠️ Sélection impossible. Réessaie.")
        return

    recap = "\n".join(f"  {i}. [{float(k['start']):.0f}-{float(k['end']):.0f}s] {(k.get('texte') or '').strip()[:60]}"
                      for i, k in enumerate(keep, 1))
    send(chat_id, f"✂️ {len(keep)} passage(s) retenu(s) :\n{recap[:3500]}\n\nDécoupe + montage (un moment)…")
    slug = project_slug(chat_id)
    dest = os.path.join(EXPORT_DIR, slug, "derush")
    os.makedirs(dest, exist_ok=True)
    out = os.path.join(dest, f"derush_{int(time.time())}.mp4")
    try:
        cut_concat(path, keep, out)
    except subprocess.CalledProcessError as e:
        err = (e.stderr.decode(errors="replace")[-300:] if e.stderr else str(e))
        send(chat_id, f"⚠️ Montage ffmpeg échoué : {err}")
        return

    url = f"{EXPORT_BASE_URL}/{slug}/derush/{os.path.basename(out)}"
    size = os.path.getsize(out)
    dur = sum(float(x["end"]) - float(x["start"]) for x in keep)
    send(chat_id, f"✅ Dérush prêt : {len(keep)} passages, ~{dur:.0f}s de montage.\n📁 {url}")
    if size <= 45 * 1024 * 1024:
        with open(out, "rb") as f:
            tg_send_video(chat_id, f.read(), caption="🎞️ Première version dérushée")
    else:
        send(chat_id, f"(vidéo {size // 1024 // 1024} Mo : trop lourde pour Telegram, récupère via le lien)")


# ---- Commandes ----
def handle_command(chat_id, parts):
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    s = session(chat_id)
    args = [p for p in parts[1:] if p.lstrip("#").isdigit()]
    ids = [int(p.lstrip("#")) for p in args]

    if cmd in ("start", "help"):
        send(chat_id, HELP)
    elif cmd == "id":
        send(chat_id, f"Ton chat_id : {chat_id}")
    elif cmd == "script":
        s["waiting_script"] = True
        send(chat_id, "📄 Envoie ton script (texte ou fichier .txt).")
    elif cmd in ("garde", "keep", "g"):
        if not ids:
            send(chat_id, "Usage : /garde <id> [id…]  (ex: /garde 3 5)")
            return
        added = []
        for i in ids:
            if i in s["props"] and i not in s["kept"]:
                s["kept"].append(i)
                added.append(i)
        persist_kept(chat_id, s)
        if added:
            send(chat_id, f"✅ Conservé : {', '.join('#' + str(i) for i in added)} "
                          f"(total {len(s['kept'])})")
        else:
            send(chat_id, "Rien d'ajouté (IDs inconnus ou déjà conservés).")
    elif cmd in ("retire", "remove", "r"):
        removed = []
        for i in ids:
            if i in s["kept"]:
                s["kept"].remove(i)
                removed.append(i)
        persist_kept(chat_id, s)
        send(chat_id, f"🗑️ Retiré : {', '.join('#' + str(i) for i in removed)}" if removed
             else "Rien à retirer.")
    elif cmd in ("export", "zip", "exportfull", "full", "complet", "downloadall"):
        export_selection(chat_id, s)
    elif cmd in ("plus", "more", "encore"):
        if not s.get("script"):
            send(chat_id, "Aucun script en mémoire. Envoie un script d'abord.")
            return
        n = ids[0] if ids else 5
        send(chat_id, f"➕ Génération de {n} segment(s) de plus…")
        avoid = [seg.get("extrait", "") for seg in s["segments"]]
        segs = segment_script(s["script"], n, avoid)
        if not segs:
            send(chat_id, "❌ Échec. Réessaie.")
            return
        append_segments(chat_id, s, segs)
    elif cmd in ("graphiques", "graphs", "graphes", "graphique"):
        handle_graphiques(chat_id, s, ids[0] if ids else None)
    elif cmd in ("anims", "animer-tout", "batch"):
        handle_anims(chat_id, s, ids[0] if ids else None)
    elif cmd in ("autoanim", "autoanime"):
        s["auto_anim"] = not s.get("auto_anim", True)
        send(chat_id, f"📊 Animations auto dans le flux : {'activées' if s['auto_anim'] else 'désactivées'}.")
    elif cmd in ("rushs", "rush"):
        handle_rushs(chat_id)
    elif cmd in ("derush", "derusher", "dérush"):
        handle_derush(chat_id, s, " ".join(parts[1:]).strip())
    elif cmd == "new":
        name = " ".join(parts[1:]).strip()
        st = chat_state(chat_id)
        base = _slug(name) if name else f"projet-{len(st['projects']) + 1}"
        slug, k = base, 2
        while slug in st["projects"]:
            slug, k = f"{base}-{k}", k + 1
        st["projects"][slug] = _new_project(name or slug)
        st["current"] = slug
        send(chat_id, f"🆕 Projet « {name or slug} » créé.\n"
                      f"📁 Dossier : {EXPORT_BASE_URL}/{slug}/\n"
                      "Envoie ton script (IDs et segments repartent à 1).")
    elif cmd in ("projets", "projects", "projet"):
        st = chat_state(chat_id)
        arg = parts[1] if len(parts) > 1 else ""
        if arg and arg in st["projects"]:
            st["current"] = arg
            send(chat_id, f"✅ Projet courant : « {st['projects'][arg].get('name', arg)} » ({arg})")
        else:
            lines = [f"📁 Projets ({len(st['projects'])}) :", ""]
            for slug, p in st["projects"].items():
                mark = "▶️" if slug == st["current"] else "  "
                lines.append(f"{mark} {slug} — {len(p.get('segments', []))} seg · {len(p.get('kept', []))} gardé(s)")
            lines += ["", "/projet <slug> pour basculer · /new <nom> pour créer"]
            send(chat_id, "\n".join(lines))
    elif cmd == "reset":
        st = chat_state(chat_id)
        cur = st["current"]
        name = st["projects"].get(cur, {}).get("name", cur)
        st["projects"][cur] = _new_project(name)
        send(chat_id, f"♻️ Projet « {name} » vidé. Envoie un nouveau script.")
    elif cmd in ("conserves", "conserve", "conservees", "liste", "kept"):
        if not s["kept"]:
            send(chat_id, "Aucun élément conservé pour l'instant.")
            return
        lines = [f"📌 Conservés ({len(s['kept'])}) :", ""]
        for i in s["kept"]:
            p = s["props"].get(i, {})
            extra = f" · {p.get('extrait')}" if p.get("extrait") else ""
            lines.append(f"#{i} · {p.get('type')}{extra}\n   {p.get('title','')[:50]}\n   {p.get('url')}")
        send(chat_id, "\n".join(lines), preview=False)
    elif cmd in ("suivant", "next", "n"):
        if not s["segments"]:
            send(chat_id, "Aucun script en cours. Envoie un script d'abord.")
            return
        s["idx"] += 1
        if s["idx"] >= len(s["segments"]):
            s["idx"] = len(s["segments"]) - 1
            send(chat_id, f"🏁 Fin des segments. {len(s['kept'])} élément(s) conservé(s). "
                          f"/conserves pour le récap.")
        else:
            process_segment(chat_id, s, s["segments"][s["idx"]])
    elif cmd in ("cherche", "recherche", "q"):
        query = " ".join(parts[1:]).strip()
        if not query:
            send(chat_id, "Usage : /cherche <termes>  (ex: /cherche datacenter servers night)")
            return
        if not s["segments"]:
            send(chat_id, "Aucun segment en cours. Envoie un script d'abord.")
            return
        seg = s["segments"][s["idx"]]
        send(chat_id, f"🔎 Nouvelle recherche — segment {s['idx'] + 1} : « {query} »")
        propose_images(chat_id, s, seg, query, s["idx"] + 1)
        propose_videos(chat_id, s, seg, query, s["idx"] + 1)
        send(chat_id, "Flag : /garde <id> · encore d'autres : /cherche <autres termes>")
    elif cmd in ("changer", "autres"):
        if not s["segments"]:
            send(chat_id, "Aucun segment en cours.")
            return
        seg = s["segments"][s["idx"]]
        pool = seg.get("img_pool", [])
        shown = seg.get("img_shown", 0)
        batch = pool[shown:shown + 3]
        if batch:
            for img in batch:
                send_image_proposal(chat_id, s, img, s["idx"] + 1)
            seg["img_shown"] = shown + len(batch)
        else:
            send(chat_id, "Plus d'images en réserve pour ce segment.")
    else:
        send(chat_id, "Commande inconnue. /help")


def append_segments(chat_id, s, new_segs):
    """Ajoute des segments à la session et reprend au premier nouveau."""
    start = len(s["segments"])
    s["segments"].extend(new_segs)
    s["idx"] = start
    send(chat_id, f"➕ {len(new_segs)} segment(s) ajouté(s) (total {len(s['segments'])}). "
                  "On reprend au premier nouveau.")
    process_segment(chat_id, s, s["segments"][s["idx"]])


def do_segment(chat_id, s, script, n):
    """Segmente puis : APPEND si une session existe, sinon initialise."""
    append = bool(s["segments"])
    send(chat_id, f"🔍 Analyse ({len(script)} car., "
                  f"{'ajout' if append else 'nouveau'}, {n or 'auto'} segments)…")
    avoid = [seg.get("extrait", "") for seg in s["segments"]] if append else None
    segs = segment_script(script, n, avoid)
    if not segs:
        send(chat_id, "❌ Impossible de segmenter. Réessaie.")
        return
    if append:
        s["script"] = (s.get("script", "") + "\n\n" + script).strip()
        append_segments(chat_id, s, segs)
    else:
        s["script"] = script
        s.update({"segments": segs, "idx": 0, "counter": 0, "props": {}, "kept": []})
        send(chat_id, f"✅ {len(segs)} segments. Flag avec /garde <id>, /suivant pour avancer.")
        process_segment(chat_id, s, segs[0])


def handle_incoming_script(chat_id, s, script):
    """Reçoit un script : demande d'abord le nombre de segments voulu."""
    s["waiting_script"] = False
    s["pending_script"] = script
    s["awaiting"] = "count"
    extra = " (s'ajoutera à la session en cours)" if s["segments"] else ""
    send(chat_id, f"📝 Script reçu ({len(script)} car.){extra}.\n"
                  "Combien de segments veux-tu ? Réponds par un nombre (ex : 10) ou « auto ».")


def consume_count(chat_id, s, text):
    """Consomme la réponse au « combien de segments ? »."""
    script = s.get("pending_script", "")
    if not script:
        s["awaiting"] = None
        return
    t = text.strip().lower()
    if t in ("auto", "a", "defaut", "défaut"):
        n = None
    else:
        m = re.search(r"\d+", t)
        if not m:
            if len(text) > 100:        # l'utilisateur a collé un nouveau script à la place
                s["pending_script"] = text
                send(chat_id, "Nouveau script reçu. Combien de segments ? (nombre, ou « auto »)")
                return
            send(chat_id, "Réponds par un nombre (ex : 10) ou « auto ».")
            return
        n = max(1, min(40, int(m.group())))
    s["awaiting"] = None
    s["pending_script"] = ""
    do_segment(chat_id, s, script, n)


def handle(msg):
    chat_id = msg["chat"]["id"]
    if ALLOWED and str(chat_id) not in ALLOWED:
        send(chat_id, "⛔ Accès non autorisé.")
        return

    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        head = text.split(None, 1)  # coupe après la commande, garde les \n du corps
        cmd0 = head[0].lstrip("/").split("@")[0].lower()
        body = head[1].strip() if len(head) > 1 else ""
        if cmd0 == "script" and body:
            handle_incoming_script(chat_id, session(chat_id), body)
            return
        if cmd0 in ("schema", "diagramme", "diagram", "graph"):
            handle_diagram(chat_id, session(chat_id), body, is_modif=False)
            return
        if cmd0 in ("modif", "modifier", "edit"):
            handle_diagram(chat_id, session(chat_id), body, is_modif=True)
            return
        if cmd0 in ("anim", "animation", "animer"):
            handle_anim(chat_id, session(chat_id), body)
            return
        if cmd0 in ("custom", "surmesure", "sur-mesure"):
            handle_custom(chat_id, session(chat_id), body)
            return
        if cmd0 in ("modcustom", "modcust", "custommod"):
            handle_custom_modif(chat_id, session(chat_id), body)
            return
        if cmd0 in ("miniatures", "miniature", "thumbnails", "thumbs", "thumb"):
            handle_thumbnails(chat_id, session(chat_id), body)
            return
        handle_command(chat_id, text.split())
        return

    s = session(chat_id)

    # Réponse au « combien de segments ? »
    if s.get("awaiting") == "count" and text:
        consume_count(chat_id, s, text)
        return

    doc = msg.get("document")
    if doc:
        mime = doc.get("mime_type") or ""
        fname = doc.get("file_name") or ""
        if "text" in mime or fname.endswith(".txt") or fname.endswith(".md"):
            try:
                fp = tg("getFile", file_id=doc["file_id"])["file_path"]
                script = requests.get(f"{TG_FILE}/{fp}", timeout=30).content.decode("utf-8", errors="replace")
                handle_incoming_script(chat_id, s, script)
            except Exception as e:
                send(chat_id, f"❌ Impossible de lire le fichier : {e}")
            return
        send(chat_id, "Envoie un fichier .txt ou .md.")
        return

    if text:
        if len(text) > 100 or s.get("waiting_script"):
            handle_incoming_script(chat_id, s, text)
        else:
            send(chat_id, "Envoie le script complet (>100 car.) ou /script puis le texte.")
        return
    send(chat_id, "/help pour commencer.")


def main():
    print(f"Bot B-Roll démarré (step-by-step). SearXNG={'oui' if SEARXNG_URL else 'non'} | "
          f"IA={'oui' if CHUTES_KEY else 'non'} | min_width={MIN_WIDTH} | "
          f"sessions={len(SESSIONS)} | allowlist={ALLOWED or 'aucune'}", flush=True)
    offset = None
    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=60, allowed_updates=["message"])
        except Exception as e:
            print(f"getUpdates erreur : {e}", file=sys.stderr)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg:
                continue
            try:
                handle(msg)
            except Exception as e:
                print(f"handle erreur : {e}", file=sys.stderr)
            finally:
                save_sessions()  # persiste l'état après chaque message


if __name__ == "__main__":
    main()
