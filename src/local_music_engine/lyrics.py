"""Shared lyric-mode checks that never rewrite the user's supplied text."""

INSTRUMENTAL_LYRICS = "[Instrumental]"


def is_instrumental_lyrics(lyrics: str) -> bool:
    """Accept the checkpoint's lowercase tag as well as the app's title case."""
    return lyrics.strip().casefold() == INSTRUMENTAL_LYRICS.casefold()
