"""Languages HomePilot can talk in.

Each language sets three things for the voice call:
  * asr     the speech-to-text language Agora listens in
  * locale/voice  the Murf Falcon voice that speaks the replies
  * reply   the instruction that makes the LLM answer in that language
Tools, the database and the dashboard stay in English; only the conversation changes.

Bhojpuri has no speech-to-text or TTS voice at the providers we use, so it listens in
Hindi (Bhojpuri speech is close enough for Hindi ASR) and answers in Bhojpuri text spoken
by a Hindi voice. Voices can be overridden per language with MURF_VOICE_<CODE>
(e.g. MURF_VOICE_ES=...) without code changes.
"""
import os

from . import config, db

LANGS = {
    "en": {"name": "English", "native": "English", "asr": None, "locale": None, "voice": None,
           "reply": "English", "greet": None},
    "hi": {"name": "Hindi", "native": "हिन्दी", "asr": "hi-IN", "locale": "hi-IN", "voice": "Namrita",
           "reply": "Hindi, written in Devanagari, natural spoken Hindi (common English words "
                    "like AC, technician, order are fine)",
           "greet": "नमस्ते! मैं होमपायलट हूँ। बताइए, आज मैं आपकी क्या मदद करूँ?"},
    "kn": {"name": "Kannada", "native": "ಕನ್ನಡ", "asr": "kn-IN", "locale": "kn-IN", "voice": "Namrita",
           "reply": "Kannada, written in Kannada script, natural spoken Kannada",
           "greet": "ನಮಸ್ಕಾರ! ನಾನು ಹೋಮ್‌ಪೈಲಟ್. ಇವತ್ತು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?"},
    "bho": {"name": "Bhojpuri", "native": "भोजपुरी", "asr": "hi-IN", "locale": "hi-IN", "voice": "Namrita",
            "reply": "Bhojpuri, written in Devanagari (e.g. 'हम', 'रउआ', 'बा', 'होई', 'का'), warm "
                     "and simple, as spoken in Bihar and eastern UP",
            "greet": "प्रणाम! हम होमपायलट हईं। बताईं, आज रउआ के का मदद करीं?"},
    "es": {"name": "Spanish", "native": "Español", "asr": "es-ES", "locale": "es-ES", "voice": "Matthew",
           "reply": "Spanish (neutral, friendly)",
           "greet": "¡Hola! Soy HomePilot. ¿En qué te ayudo hoy?"},
}


def current() -> str:
    code = db.recall("language") or "en"
    return code if code in LANGS else "en"


def set_current(code: str) -> str:
    code = code if code in LANGS else "en"
    db.remember("language", code)
    db.log("system", f"Language: {LANGS[code]['name']}", LANGS[code]["native"])
    return code


def voice_settings(code: str) -> dict:
    """ASR language, Murf voice and locale for a language (English = the configured default)."""
    L = LANGS.get(code, LANGS["en"])
    if code == "en":
        return {"asr": config.AGORA_ASR_LANGUAGE, "voice": config.MURF_VOICE_ID,
                "locale": config.MURF_LOCALE}
    return {"asr": os.getenv(f"AGORA_ASR_{code.upper()}", L["asr"]),
            "voice": os.getenv(f"MURF_VOICE_{code.upper()}", L["voice"]),
            "locale": os.getenv(f"MURF_LOCALE_{code.upper()}", L["locale"])}


def instruction(code: str | None = None) -> str:
    code = code or current()
    if code == "en":
        return ""
    L = LANGS[code]
    return (f"LANGUAGE: the family is speaking {L['name']}. Reply ONLY in {L['reply']}. "
            f"Understand {L['name']}, Hindi or English input. Keep names (Anika, Rohan, Kabir, "
            f"Dadi) as they are and say times and rupee amounts the way a {L['name']} speaker "
            f"would. Tool arguments (titles, items) stay in English.")


def options() -> list[dict]:
    return [{"code": k, "name": v["name"], "native": v["native"]} for k, v in LANGS.items()]
