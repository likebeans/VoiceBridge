"""Language capabilities for exact model IDs, checked against realtime API docs.

Sources (2026-11-04):
https://help.aliyun.com/zh/model-studio/fun-asr-client-events
https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-client-events

The service uses `fil` for Filipino. Fun-ASR calls it `tl` on the wire. Capabilities
are documented model coverage, not a guarantee that a proxy applies every hint.
Unknown/custom model IDs have no advertised list and cannot be fallback targets.
"""

import re
from dataclasses import dataclass

_NAMES = {
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "it": "意大利语",
    "vi": "越南语",
    "th": "泰语",
    "id": "印尼语",
    "ms": "马来语",
    "fil": "菲律宾语",
    "hi": "印地语",
    "nl": "荷兰语",
    "sv": "瑞典语",
    "da": "丹麦语",
    "fi": "芬兰语",
    "no": "挪威语",
    "el": "希腊语",
    "pl": "波兰语",
    "cs": "捷克语",
    "hu": "匈牙利语",
    "ro": "罗马尼亚语",
    "bg": "保加利亚语",
    "hr": "克罗地亚语",
    "sk": "斯洛伐克语",
    "yue": "粤语",
    "tr": "土耳其语",
    "uk": "乌克兰语",
    "is": "冰岛语",
}

# Deliberate aliases only: truncating any BCP-47 tag to its first component can
# erase meaningful distinctions. A Chinese locale is not a conversion request.
_ALIASES = {
    "tl": "fil",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "en-us": "en",
    "en-gb": "en",
    "en-au": "en",
    "ja-jp": "ja",
    "ko-kr": "ko",
    "fr-fr": "fr",
    "fr-ca": "fr",
    "de-de": "de",
    "es-es": "es",
    "es-mx": "es",
    "pt-br": "pt",
    "pt-pt": "pt",
    "ru-ru": "ru",
    "vi-vn": "vi",
    "th-th": "th",
    "id-id": "id",
    "hi-in": "hi",
    "fil-ph": "fil",
    "tl-ph": "fil",
}
_LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-[a-z]{2,8})*")


def normalize_language(value: object) -> str | None:
    """Normalize our public language field; never send the literal `auto` upstream."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Language must be a language code or null")
    value = value.strip().lower()
    if value == "auto":
        return None
    if len(value) > 16 or not _LANGUAGE_TAG.fullmatch(value):
        raise ValueError("Use a language code such as ja or fr, or null for auto detection")
    return _ALIASES.get(value, value)


@dataclass(frozen=True)
class LanguageProfile:
    codes: tuple[str, ...]

    def metadata(self) -> dict:
        return {
            "languages": [{"code": code, "name": _NAMES[code]} for code in self.codes],
            "auto_detect": True,
            "language_support_known": True,
        }


_FUN = LanguageProfile(
    tuple(
        (
            "zh en ja ko vi th id ms fil hi ar fr de es pt ru it "
            "nl sv da fi no el pl cs hu ro bg hr sk"
        ).split()
    )
)
_QWEN = LanguageProfile(
    tuple(
        "zh yue en ja de ko ru fr pt ar it es hi id th tr uk vi cs da fil fi is ms no pl sv".split()
    )
)
_PROFILES = {
    ("funasr", "fun-asr-realtime"): _FUN,
    ("funasr", "fun-asr-realtime-2026-02-28"): LanguageProfile(("zh", "en", "ja")),
    ("qwen", "qwen3-asr-flash-realtime"): _QWEN,
}


def profile_for(provider: str, model: str) -> LanguageProfile | None:
    return _PROFILES.get((provider, model))


def language_metadata(provider: str, model: str) -> dict:
    profile = profile_for(provider, model)
    if profile is not None:
        return profile.metadata()
    return {"languages": [], "auto_detect": None, "language_support_known": False}


def wire_language(provider: str, language: str | None) -> str | None:
    """Convert a normalized service code to the provider's own spelling."""
    return "tl" if provider == "funasr" and language == "fil" else language


def can_fallback(
    primary: LanguageProfile | None, candidate: LanguageProfile | None, language: str | None
) -> bool:
    if candidate is None:
        return False
    if language is not None:
        return language in candidate.codes
    # In automatic mode we do not yet know the spoken language. A fallback must
    # retain the primary's entire promised coverage, not just support auto itself.
    return primary is not None and set(primary.codes).issubset(candidate.codes)
