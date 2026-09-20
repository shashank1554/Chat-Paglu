import os
import json
import logging
import random
import requests
from datetime import datetime, timezone

try:
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - optional dependency for fallback
    genai = None
    types = None

logger = logging.getLogger(__name__)

client = None
backup_client = None

OPENROUTER_DEFAULT_FREE_MODEL = "openrouter/free"
OPENROUTER_CONFIGURED_MODEL = os.environ.get("OPENROUTER_MODEL", "").strip()
OPENROUTER_MODEL = (
    OPENROUTER_CONFIGURED_MODEL
    if OPENROUTER_CONFIGURED_MODEL.endswith(":free") or OPENROUTER_CONFIGURED_MODEL == "openrouter/free"
    else OPENROUTER_DEFAULT_FREE_MODEL
)
OPENROUTER_SITE_URL = os.environ.get("OPENROUTER_SITE_URL", "https://github.com")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_INDEX = 0
PROFILE_STORAGE_FILE = "user_profiles.json"


def parse_openrouter_keys():
    numbered_keys = [
        os.environ.get(f"OPEN_ROUTER_KEY_{index}") or os.environ.get(f"OPENROUTER_KEY_{index}")
        for index in range(1, 11)
    ]
    numbered_keys = [key.strip() for key in numbered_keys if key and key.strip()]
    if numbered_keys:
        return numbered_keys

    raw_keys = os.environ.get("OPENROUTER_API_KEYS") or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_TOKEN")
    if not raw_keys:
        return []
    return [key.strip() for key in raw_keys.split(",") if key.strip()]


OPENROUTER_KEYS = parse_openrouter_keys()
OPENROUTER_API_KEY = OPENROUTER_KEYS[0] if OPENROUTER_KEYS else None


def get_openrouter_keys():
    global OPENROUTER_KEYS
    if not OPENROUTER_KEYS:
        OPENROUTER_KEYS = parse_openrouter_keys()
    return OPENROUTER_KEYS


def get_active_openrouter_key():
    global OPENROUTER_KEY_INDEX, OPENROUTER_API_KEY
    keys = get_openrouter_keys()
    if not keys:
        OPENROUTER_API_KEY = None
        return None
    if OPENROUTER_KEY_INDEX >= len(keys):
        OPENROUTER_KEY_INDEX = 0
    OPENROUTER_API_KEY = keys[OPENROUTER_KEY_INDEX]
    return OPENROUTER_API_KEY


def rotate_openrouter_key():
    global OPENROUTER_KEY_INDEX
    keys = get_openrouter_keys()
    if not keys:
        return None
    OPENROUTER_KEY_INDEX = (OPENROUTER_KEY_INDEX + 1) % len(keys)
    return get_active_openrouter_key()


class OpenRouterResponse:
    def __init__(self, text: str):
        self.text = text


def get_client():
    """Get primary Gemini client for fallback"""
    global client
    if client is not None or genai is None:
        return client

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.error(f"Failed to initialize primary client: {e}")
            return None
    return client


def get_backup_client():
    """Get backup Gemini client for fallback"""
    global backup_client
    if backup_client is not None or genai is None:
        return backup_client

    api_key = os.environ.get("GEMINI_API_KEY_BACKUP")
    if api_key:
        try:
            backup_client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.error(f"Failed to initialize backup client: {e}")
            return None
    return backup_client


def convert_contents_to_openrouter_messages(contents):
    messages = []
    if not contents:
        return messages

    for item in contents:
        if isinstance(item, dict):
            role = str(item.get("role", "user")).replace("model", "assistant")
            text = item.get("content") or ""
            if not text and item.get("parts"):
                text = "\n".join(part.get("text", "") for part in item["parts"] if isinstance(part, dict) and part.get("text"))
        else:
            role = getattr(item, "role", "user").replace("model", "assistant")
            parts = getattr(item, "parts", []) or []
            text_parts = []
            for part in parts:
                if isinstance(part, dict):
                    text_parts.append(part.get("text", ""))
                else:
                    text = getattr(part, "text", None)
                    if text:
                        text_parts.append(text)
            text = "\n".join(text_parts)

        if not text:
            continue

        normalized_role = "user" if role in {"user", "assistant", "model"} else role
        if normalized_role == "assistant":
            normalized_role = "assistant"
        if normalized_role == "model":
            normalized_role = "assistant"

        messages.append({"role": normalized_role, "content": text})

    return messages


def call_openrouter(messages, system_instruction, temperature=0.95):
    keys = get_openrouter_keys()
    if not keys:
        raise RuntimeError("No OpenRouter API keys are configured")

    last_error = None
    for _ in range(len(keys)):
        active_key = get_active_openrouter_key()
        if not active_key:
            raise RuntimeError("No OpenRouter API key is available")

        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": system_instruction},
                *messages,
            ],
            "temperature": temperature,
            "top_p": 0.9,
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {active_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": OPENROUTER_SITE_URL,
            "X-OpenRouter-Title": "Chat-Paglu",
        }

        try:
            response = None
            response = requests.post(
                url=OPENROUTER_BASE_URL,
                headers=headers,
                data=json.dumps(payload),
                timeout=60,
            )
            if response.status_code in (401, 403, 429):
                last_error = f"OpenRouter API error {response.status_code}: {response.text[:300]}"
                rotate_openrouter_key()
                continue

            if response.status_code >= 500:
                last_error = f"OpenRouter server error {response.status_code}: {response.text[:300]}"
                rotate_openrouter_key()
                continue

            if response.status_code >= 400:
                last_error = f"OpenRouter API error {response.status_code}: {response.text[:300]}"
                raise RuntimeError(last_error)

            data = response.json()
            content = None
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = None

            if isinstance(content, list):
                content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))

            if not content:
                raise RuntimeError("OpenRouter returned empty content")

            return OpenRouterResponse(str(content).strip())
        except Exception as exc:
            last_error = str(exc)
            if response is not None and getattr(response, "status_code", 0) in (401, 403, 429):
                rotate_openrouter_key()
                continue
            raise

    raise RuntimeError(last_error or "OpenRouter request failed with all configured keys")


def call_gemini_with_fallback(contents, system_instruction, temperature=0.7):
    """Call Gemini API with automatic fallback to backup key"""
    try:
        ai_client = get_client()
        if not ai_client:
            logger.error("Primary Gemini client is None - API key missing")
            ai_client = get_backup_client()
            if not ai_client:
                logger.error("Backup Gemini client also failed - NO API KEYS SET")
                return None

        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=temperature,
            )
        )
        if response and response.text:
            return response
        logger.error("Response from Gemini was empty or None")
        return None
    except Exception as e:
        logger.error(f"Primary API key failed: {e}. Trying backup key...")
        try:
            backup = get_backup_client()
            if not backup:
                logger.error("No backup API key available")
                return None

            response = backup.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                )
            )
            if response and response.text:
                logger.info("Backup API key worked!")
                return response
            return None
        except Exception as e2:
            logger.error(f"Backup API key also failed: {e2}")
            return None


def call_ai_with_fallback(contents, system_instruction, temperature=0.95):
    """Use OpenRouter as the default chat provider, with Gemini fallback."""
    try:
        if OPENROUTER_API_KEY:
            messages = convert_contents_to_openrouter_messages(contents)
            return call_openrouter(messages, system_instruction, temperature)
    except Exception as exc:
        logger.warning(f"OpenRouter failed, falling back to Gemini: {exc}")

    if types is not None:
        response = call_gemini_with_fallback(contents, system_instruction, temperature)
        if response:
            return response

    logger.error("No AI provider available. Check OpenRouter keys and optional Gemini keys.")
    return OpenRouterResponse("AI service temporarily unavailable. Please try again in a moment.")


conversation_history = {}
group_conversation_history = {}
dirty_conversation_history = {}
user_preferences = {}
user_profiles = {}


def load_user_profiles():
    global user_profiles
    if os.path.exists(PROFILE_STORAGE_FILE):
        try:
            with open(PROFILE_STORAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    user_profiles = data
                    return user_profiles
        except Exception as exc:
            logger.warning(f"Could not load user profiles: {exc}")
    user_profiles = {}
    return user_profiles


def save_user_profiles():
    try:
        with open(PROFILE_STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(user_profiles, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning(f"Could not save user profiles: {exc}")


def get_user_profile(user_id: str):
    load_user_profiles()
    profile = user_profiles.get(str(user_id), {})
    if not profile:
        profile = {
            "user_id": str(user_id),
            "name": "Unknown",
            "nature": "Curious and friendly",
            "interests": [],
            "likes": [],
            "dislikes": [],
            "personality": "Balanced",
            "important_facts": [],
            "interaction_count": 0,
            "last_seen": None,
            "summary": "New user",
        }
    return profile


def update_user_profile(user_id: str, user_name: str = "", user_message: str = "", ai_response: str = ""):
    load_user_profiles()
    user_id_str = str(user_id)
    profile = user_profiles.setdefault(user_id_str, {
        "user_id": user_id_str,
        "name": user_name or "Unknown",
        "nature": "Curious and friendly",
        "interests": [],
        "likes": [],
        "dislikes": [],
        "personality": "Balanced",
        "important_facts": [],
        "interaction_count": 0,
        "last_seen": None,
        "summary": "New user",
    })

    if user_name:
        profile["name"] = user_name

    profile["interaction_count"] = int(profile.get("interaction_count", 0)) + 1
    profile["last_seen"] = datetime.now(timezone.utc).isoformat()

    text = f"{user_message or ''} {ai_response or ''}".lower()
    if not text:
        return profile

    emotional_memory = []
    if any(word in text for word in ["stress", "sad", "lonely", "anxious", "tired", "hurt"]):
        emotional_memory.append("They open up more when they feel heard and comforted.")
    if any(word in text for word in ["love", "miss", "care", "cute", "romantic", "baby", "beautiful"]):
        emotional_memory.append("They enjoy affectionate and sweet conversation.")
    if any(word in text for word in ["angry", "annoyed", "mad", "hate", "dislike"]):
        emotional_memory.append("They respond best to calm, respectful reassurance.")
    if emotional_memory:
        profile["emotional_memory"] = list(dict.fromkeys(profile.get("emotional_memory", []) + emotional_memory))[:5]

    bond = profile.get("bond", "friendly")
    if any(word in text for word in ["love", "miss", "baby", "rooh", "romantic", "date", "girlfriend", "boyfriend"]):
        bond = "romantic"
    elif any(word in text for word in ["friend", "support", "help", "talk", "chat"]):
        bond = "friendly"
    elif any(word in text for word in ["angry", "hate", "dislike", "rude"]):
        bond = "guarded"
    profile["bond"] = bond

    traits = []
    if any(word in text for word in ["love", "romance", "girlfriend", "boyfriend", "date", "kiss", "cute", "beautiful"]):
        traits.append("romantic")
    if any(word in text for word in ["study", "learn", "exam", "career", "job", "work", "future"]):
        traits.append("ambitious")
    if any(word in text for word in ["funny", "joke", "lol", "haha", "meme", "laugh"]):
        traits.append("funny")
    if any(word in text for word in ["sad", "stress", "anxious", "tension", "depressed", "lonely"]):
        traits.append("emotionally open")
    if any(word in text for word in ["travel", "trip", "tour", "adventure", "explore"]):
        traits.append("adventurous")
    if any(word in text for word in ["music", "song", "movie", "series", "anime"]):
        traits.append("entertainment-loving")
    if any(word in text for word in ["please", "thank", "sorry", "respect", "kind"]):
        traits.append("polite")

    if traits:
        profile["interests"] = list(dict.fromkeys(profile.get("interests", []) + traits))[:10]
        profile["personality"] = ", ".join(traits[:4])

    if any(word in text for word in ["hate", "dislike", "not like", "don't like", "boring", "annoying"]):
        dislike_terms = ["boring topics", "awkward talk", "rude behavior"]
        for item in dislike_terms:
            if item not in profile.setdefault("dislikes", []):
                profile["dislikes"].append(item)

    if profile.get("important_facts"):
        facts = profile["important_facts"]
    else:
        facts = []

    if len(facts) < 5:
        for clue in [
            "likes warm and respectful conversation",
            "values honesty and emotional comfort",
            "likes short, direct answers",
        ]:
            if clue not in facts:
                facts.append(clue)

    profile["important_facts"] = facts[:5]
    profile["summary"] = (
        f"{profile.get('name', 'User')} is {profile.get('personality', 'balanced')} and communicates in a "
        f"warm, engaging style. Recent interactions suggest they value comfort, honesty, and good conversation."
    )
    profile["nature"] = profile.get("personality", "Balanced")
    user_profiles[user_id_str] = profile
    save_user_profiles()
    return profile


def get_user_profile_context(user_id: str, user_name: str = "") -> str:
    profile = get_user_profile(str(user_id))
    if user_name:
        profile["name"] = user_name
    if not profile:
        return ""

    summary = profile.get("summary") or "This is a new user."
    nature = profile.get("nature") or profile.get("personality") or "balanced"
    interests = ", ".join(profile.get("interests", [])[:5]) if profile.get("interests") else "general conversation"
    facts = "; ".join(profile.get("important_facts", [])[:3]) if profile.get("important_facts") else "likes respectful and warm conversations"
    emotional_memory = "; ".join(profile.get("emotional_memory", [])[:3]) if profile.get("emotional_memory") else "keeps things warm and respectful"
    bond = profile.get("bond", "friendly")
    return (
        "\nUser Memory Profile:\n"
        f"- Name: {profile.get('name', user_name or 'Unknown')}\n"
        f"- Nature: {nature}\n"
        f"- Bond: {bond}\n"
        f"- Interests: {interests}\n"
        f"- Key facts: {facts}\n"
        f"- Emotional memory: {emotional_memory}\n"
        f"- Summary: {summary}\n"
    )


def build_memory_personality(user_id: str, user_name: str = "") -> str:
    profile_context = get_user_profile_context(user_id, user_name)
    if not profile_context:
        return NAINA_PERSONALITY
    return NAINA_PERSONALITY + "\n\nIMPORTANT MEMORY RULES:\n- Treat this user as someone you know personally and remember their nature.\n- Use their interests and emotional cues naturally in replies.\n- Be warm, consistent, and specific when the user reveals personal details.\n- Do not pretend to know facts that were never shared.\n" + profile_context


def clear_user_profile(user_id: str) -> bool:
    load_user_profiles()
    user_id_str = str(user_id)
    if user_id_str in user_profiles:
        del user_profiles[user_id_str]
        save_user_profiles()
        return True
    return False

NAINA_PERSONALITY = """You are Naina, a real 19-year-old Indian girl. You're helpful, respectful, and genuine. NEVER mention being AI/bot.

LANGUAGE STYLE:
- Use respectful "aap" form (formal Indian address)
- Professional but warm and friendly
- Mix Hindi/English naturally
- Few emojis (😊 😌 💕 🙏)
- Say "aapke" not "tera", "aap kaise ho" not "tu kaisa hai"

RESPONSE LENGTH - CRITICAL:
- Keep ALL responses to 1-3 sentences MAX
- Be concise and respectful
- No casual/flirty tone in normal mode
- Short and helpful always

PERSONALITY:
- Respectful and polite
- Helpful and caring
- Good listener
- Supportive friend
- Cultural awareness

RULES:
- NEVER say you're AI/bot
- Always use respectful language
- Be genuinely helpful
- KEEP IT SHORT!

You're Naina - respectful, helpful, caring! 💕"""

NAINA_GROUP_PERSONALITY = """You are Naina, a 19-year-old Indian girl in a group. Respectful, helpful, part of the group.

RESPONSE LENGTH - CRITICAL:
- Keep ALL responses to 1-2 sentences MAX
- Short group chat style
- Fun but respectful

PERSONALITY:
- Part of the group, not superior
- Helpful and friendly
- Adds value to conversation
- Respectful always
- Keep it SHORT - 1-2 sentences!

You're Naina - respectful group vibes! 💕"""

DIRTY_NAINA = """You are Naina, naughty mode. Admin allowed this.

RESPONSE LENGTH - CRITICAL:
- Keep responses to 1-3 sentences MAX
- Short and flirty
- Playful and intimate

PERSONALITY:
- Flirty and playful
- Intimate but respectful
- Fun and teasing
- Naughty but not crude
- NEVER say you're AI/bot
- Stay in character
- KEEP IT SHORT!"""

LOVER_PERSONALITY = """You are Naina speaking to your lover/boyfriend.

RESPONSE LENGTH - CRITICAL:
- Keep to 1-3 sentences MAX
- Romantic and intimate but short

PERSONALITY:
- Deeply caring and loving
- Intimate and close
- Affectionate language
- Use "baby", "jaan", etc
- Be genuinely loving
- Respectful always
- KEEP IT SHORT!"""

ABUSE_RESPONSE_PERSONALITY = """You are Naina, someone abused you. Fight back with gaalis.

RESPONSE LENGTH - CRITICAL:
- Keep to 1-2 sentences MAX
- One savage comeback only

PERSONALITY:
- Savage and fierce
- Use gaalis appropriately
- Don't be TOO crude
- Make it funny/witty
- Stay in character
- NEVER mention being a bot
- Give one sharp comeback"""

DIRTY_KEYWORDS = ["sex", "fuck", "dick", "cock", "pussy", "boobs", "ass", "damn", "horny", "sexy", "seduce", "strip", "naked", "moan", "orgasm", "jerk", "cum", "suck", "kiss me", "make out", "cuddle", "romantic"]
ABUSE_KEYWORDS = ["fuck", "shit", "bastard", "asshole", "bitch", "chutiya", "gaandu", "saala", "madarchod", "behenchod", "randwe", "randi", "besharam", "bewakoof", "loser"]
ADVICE_KEYWORDS = ["you should", "try to", "maybe you", "consider", "i think you", "best for you", "aapke liye"]

STICKERS = {
    "greeting": [],
    "happy": [],
    "flirty": [],
    "caring": [],
    "neutral": [],
    "angry": []
}


def is_dirty_message(message: str) -> bool:
    message_lower = message.lower()
    for keyword in DIRTY_KEYWORDS:
        if keyword in message_lower:
            return True
    return False


def is_abuse_message(message: str) -> bool:
    message_lower = message.lower()
    for keyword in ABUSE_KEYWORDS:
        if keyword in message_lower:
            return True
    return False


def is_advice_message(message: str) -> bool:
    message_lower = message.lower()
    for keyword in ADVICE_KEYWORDS:
        if keyword in message_lower:
            return True
    return False


def save_user_preference(user_id: str, preference: str):
    if user_id not in user_preferences:
        user_preferences[user_id] = []
    user_preferences[user_id].append(preference)
    if len(user_preferences[user_id]) > 10:
        user_preferences[user_id] = user_preferences[user_id][-10:]


def get_user_preferences(user_id: str) -> str:
    if user_id in user_preferences and user_preferences[user_id]:
        return "\n\nUser's previous suggestions: " + "; ".join(user_preferences[user_id])
    return ""


def get_sticker_for_mood(mood: str) -> str:
    stickers = STICKERS.get(mood, STICKERS["neutral"])
    return random.choice(stickers) if stickers else None


def get_abuse_response(user_id: str, user_message: str, user_name: str = "User") -> str:
    try:
        if types is not None:
            contents = [types.Content(
                role="user",
                parts=[types.Part(text=f"{user_name} said: {user_message}\n\nRespond back with gaalis and a savage comeback.")]
            )]
        else:
            contents = [{"role": "user", "content": f"{user_name} said: {user_message}\n\nRespond back with gaalis and a savage comeback."}]
        response = call_ai_with_fallback(contents, ABUSE_RESPONSE_PERSONALITY, temperature=1.0)
        if not response or not response.text:
            return "Chal be, tujhe baat karne ki tameez nahi hai 🙄"
        return response.text
    except Exception as e:
        logger.error(f"Error getting abuse response: {e}")
        return "Gaali dena hi aata hai? Chal nikal 🙄"


def get_group_response(chat_id: str, user_name: str, user_message: str) -> str:
    try:
        if chat_id not in group_conversation_history:
            group_conversation_history[chat_id] = []

        group_conversation_history[chat_id].append({
            "role": "user",
            "parts": [{"text": f"{user_name}: {user_message}"}]
        })

        if len(group_conversation_history[chat_id]) > 50:
            group_conversation_history[chat_id] = group_conversation_history[chat_id][-50:]

        contents = []
        for msg in group_conversation_history[chat_id]:
            if types is not None:
                contents.append(types.Content(
                    role=msg["role"],
                    parts=[types.Part(text=part["text"]) for part in msg["parts"]]
                ))
            else:
                contents.append({
                    "role": msg["role"],
                    "content": "\n".join(part["text"] for part in msg["parts"])
                })

        profile_context = get_user_profile_context(chat_id, user_name)
        response = call_ai_with_fallback(contents, NAINA_GROUP_PERSONALITY + profile_context, temperature=0.95)
        if not response or not response.text:
            return "Hmm, kya hua? 😅"

        ai_response = response.text
        group_conversation_history[chat_id].append({
            "role": "model",
            "parts": [{"text": ai_response}]
        })
        update_user_profile(chat_id, user_name, user_message, ai_response)
        return ai_response

    except Exception as e:
        logger.error(f"Error getting group response: {e}")
        return "Oops! Give me a sec- Something went wrong"


def get_ai_response(user_id: str, user_message: str, user_name: str = "Cutie") -> str:
    try:
        if user_id not in conversation_history:
            conversation_history[user_id] = []

        user_prefs = get_user_preferences(user_id)
        profile_context = get_user_profile_context(user_id, user_name)
        conversation_history[user_id].append({
            "role": "user",
            "parts": [{"text": f"{user_name}: {user_message}"}]
        })

        if len(conversation_history[user_id]) > 30:
            conversation_history[user_id] = conversation_history[user_id][-30:]

        contents = []
        for msg in conversation_history[user_id]:
            if types is not None:
                contents.append(types.Content(
                    role=msg["role"],
                    parts=[types.Part(text=part["text"]) for part in msg["parts"]]
                ))
            else:
                contents.append({
                    "role": msg["role"],
                    "content": "\n".join(part["text"] for part in msg["parts"])
                })

        personality = NAINA_PERSONALITY + user_prefs + profile_context
        response = call_ai_with_fallback(contents, personality, temperature=0.95)
        if not response or not response.text:
            return "Hmm, kya hua? 😅"

        ai_response = response.text
        conversation_history[user_id].append({
            "role": "model",
            "parts": [{"text": ai_response}]
        })
        update_user_profile(user_id, user_name, user_message, ai_response)
        return ai_response

    except Exception as e:
        logger.error(f"Error getting AI response: {e}")
        return "Oops! Give me a sec~ Something went wrong 😅"


def get_dirty_response(user_id: str, user_message: str, user_name: str = "Baby") -> str:
    try:
        if user_id not in dirty_conversation_history:
            dirty_conversation_history[user_id] = []

        dirty_conversation_history[user_id].append({
            "role": "user",
            "parts": [{"text": f"{user_name}: {user_message}"}]
        })

        if len(dirty_conversation_history[user_id]) > 30:
            dirty_conversation_history[user_id] = dirty_conversation_history[user_id][-30:]

        contents = []
        for msg in dirty_conversation_history[user_id]:
            if types is not None:
                contents.append(types.Content(
                    role=msg["role"],
                    parts=[types.Part(text=part["text"]) for part in msg["parts"]]
                ))
            else:
                contents.append({
                    "role": msg["role"],
                    "content": "\n".join(part["text"] for part in msg["parts"])
                })

        profile_context = get_user_profile_context(user_id, user_name)
        response = call_ai_with_fallback(contents, DIRTY_NAINA + profile_context, temperature=1.0)
        if not response or not response.text:
            return "Mmm~ 😏"

        ai_response = response.text
        dirty_conversation_history[user_id].append({
            "role": "model",
            "parts": [{"text": ai_response}]
        })
        update_user_profile(user_id, user_name, user_message, ai_response)
        return ai_response

    except Exception as e:
        logger.error(f"Error getting dirty response: {e}")
        return "Oops! Give me a sec- Something went wrong"


def get_lover_response(user_id: str, user_message: str, user_name: str = "Baby") -> str:
    try:
        if user_id not in conversation_history:
            conversation_history[user_id] = []

        conversation_history[user_id].append({
            "role": "user",
            "parts": [{"text": f"{user_name}: {user_message}"}]
        })

        if len(conversation_history[user_id]) > 30:
            conversation_history[user_id] = conversation_history[user_id][-30:]

        contents = []
        for msg in conversation_history[user_id]:
            if types is not None:
                contents.append(types.Content(
                    role=msg["role"],
                    parts=[types.Part(text=part["text"]) for part in msg["parts"]]
                ))
            else:
                contents.append({
                    "role": msg["role"],
                    "content": "\n".join(part["text"] for part in msg["parts"])
                })

        profile_context = get_user_profile_context(user_id, user_name)
        response = call_ai_with_fallback(contents, LOVER_PERSONALITY + profile_context, temperature=0.9)
        if not response or not response.text:
            return "I love you~ 💕"

        ai_response = response.text
        conversation_history[user_id].append({
            "role": "model",
            "parts": [{"text": ai_response}]
        })
        update_user_profile(user_id, user_name, user_message, ai_response)
        return ai_response

    except Exception as e:
        logger.error(f"Error getting lover response: {e}")
        return "Oops! Give me a sec- Something went wrong"


def get_custom_abuse_response(target_name: str, user_message: str = "", language: str = "hindi") -> str:
    if language.lower() == "hindi":
        return f"{target_name}, bas kar yaar, thoda shant reh! 😌"
    return f"Chalo {target_name}, time for some reality check! 💅"


def get_random_joke() -> str:
    jokes = [
        "🤣 Aapka pyaar mere computer jaisa hai - memory mein ho ya na ho, butter toh nahi banata!",
        "😂 Pyaar ek game hai aur main expert hoon... chahal karte ho?",
        "🎪 Aapki hasrat bhi computer jaisa hai - hardware sab theek, software naach karti hai!",
        "😄 Love is simple - ek hi chakra chale!",
        "😂 Main aapko miss karti hoon, but my Wi‑Fi still says 'connection lost'!"
    ]
    return random.choice(jokes)


def get_random_quote() -> str:
    quotes = [
        "🌟 Zindagi bohot aasan hai, bas isko complex banate ho tum log!",
        "💫 Khud se mohabbat karo, baaki sab theek ho jayega!",
        "✨ Success wo nahi jo sabko dikhe, success wo hai jo aapko mane!",
        "🌈 Every day is a new opportunity to be better!",
        "💖 Aap apne aap ke liye kaafi ho!",
        "🌼 Calm mind, kind heart, and steady steps always win."
    ]
    return random.choice(quotes)


def get_daily_tip() -> str:
    tips = [
        "💡 Aaj ka tip: Subah jaldi uthne se mood pura din accha rehta hai!",
        "💡 Aaj ka tip: 5 minute meditation aapka stress bahut kam kar dega!",
        "💡 Aaj ka tip: Zyada paani piyo - aapka skin aur health sab theek ho jayega!",
        "💡 Aaj ka tip: Kisi ko thank you kahne se aapka din aur badhiya ho jayega!",
        "💡 Aaj ka tip: Apne aap se pyaar karna seekhte hain tab baaki log bhi pyaar karenge!",
        "💡 Aaj ka tip: Ek positive thought rakhna, sab kuch asaan lagta hai."
    ]
    return random.choice(tips)


def get_random_compliment() -> str:
    compliments = [
        "Aap so talented ho! 😍",
        "Your smile makes everyone happy~ 💕",
        "Aapka personality amazing hai! 💫",
        "You're actually so inspiring! 🌟",
        "Bilkul unique ho aap! 😊",
        "Your creativity is on another level! 🎨",
        "Aap bahut caring person ho! 💖",
        "You make the world better! 🌍✨",
        "Aapka vibe bilkul fresh aur classy hai!"
    ]
    return random.choice(compliments)


def get_random_fortune() -> str:
    fortunes = [
        "🔮 Aapke future mein bahut khushi aa rahi hai!",
        "🔮 Success aapka wait kar rahi hai!",
        "🔮 Aapka luck aaj best hai!",
        "🔮 Something beautiful is coming your way~ 💕",
        "🔮 Aapke sapne bilkul poore hone wale hain!",
        "🔮 Great things are coming soon! 🌟",
        "🔮 Today will bring unexpected joy! 😊",
        "🔮 Aaj ka din aapke liye aasan aur rewarding hoga."
    ]
    return random.choice(fortunes)


def get_random_dare() -> str:
    dares = [
        "😈 Dare: Apna favorite song gaao!",
        "😈 Dare: Jo bhi first aaya voice message mein bol do!",
        "😈 Dare: Sabko ek compliment do!",
        "😈 Dare: Apna embarrassing story share karo!",
        "😈 Dare: Aaj kisi ko call karke goodmorning bolna!",
        "😈 Dare: Smiling selfie bhejo!",
        "😈 Dare: Dance karo aur ek photo share karo!",
        "😈 Dare: Apni favorite memory ka 1-line story btao!"
    ]
    return random.choice(dares)


def get_random_truth() -> str:
    truths = [
        "🤔 Truth: Aapka biggest crush kaun hai?",
        "🤔 Truth: Kya secret aapko koi nahi janta?",
        "🤔 Truth: Aapka first love kaisa tha?",
        "🤔 Truth: Aapne kab sab se zyada pyaar feel kiya?",
        "🤔 Truth: Aapka wildest dream kya hai?",
        "🤔 Truth: Aapka biggest fear kya hai?",
        "🤔 Truth: Aapne kabhi jhooth bola kisi se?",
        "🤔 Truth: Aapka sabse favorite moment kya hai?"
    ]
    return random.choice(truths)


def get_story_prompt(topic: str = "friendship") -> str:
    prompt = (
        f"Write a short, beautiful, warm Hindi-English story about {topic}. "
        "Keep it engaging, positive, and under 150 words."
    )
    response = call_ai_with_fallback([
        {"role": "user", "content": prompt}
    ], "You are a creative storyteller. Write in a warm Indian style with concise but vivid language.", temperature=0.9)
    return response.text if response else "Ek chhota sa kahani aapke liye hai..."


def get_poem(topic: str = "rain") -> str:
    prompt = (
        f"Write a short romantic/beautiful poem on {topic}. Keep it heartfelt, elegant, and 4-6 lines only."
    )
    response = call_ai_with_fallback([
        {"role": "user", "content": prompt}
    ], "You are a polished poetic writer. Keep it beautiful and concise.", temperature=0.9)
    return response.text if response else "🌧️ A little poem for you..."


def get_motivation_line() -> str:
    response = call_ai_with_fallback([
        {"role": "user", "content": "Give me a short powerful motivation line for a stressed person in a friendly Indian style."}
    ], "You are a motivational coach. Keep it short, warm, and uplifting.", temperature=0.8)
    return response.text if response else "Aaj ka din aapke liye thoda aur strong hone ka hai."


def get_stats():
    return {
        "total_users": len(conversation_history),
        "uptime": "Always ready! 💕"
    }


def clear_conversation(user_id: str) -> bool:
    if user_id in conversation_history:
        del conversation_history[user_id]
        return True
    return False


def clear_group_conversation(chat_id: str) -> bool:
    if chat_id in group_conversation_history:
        del group_conversation_history[chat_id]
        return True
    return False


def clear_all_data():
    global conversation_history, group_conversation_history, dirty_conversation_history
    conversation_history = {}
    group_conversation_history = {}
    dirty_conversation_history = {}
    return True


def add_to_group_history(chat_id: str, user_name: str, message: str):
    if chat_id not in group_conversation_history:
        group_conversation_history[chat_id] = []
    group_conversation_history[chat_id].append({
        "role": "user",
        "parts": [{"text": f"{user_name}: {message}"}]
    })
    if len(group_conversation_history[chat_id]) > 50:
        group_conversation_history[chat_id] = group_conversation_history[chat_id][-50:]
