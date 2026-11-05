from app.matching import best_match, build_query, clean_title, score
from app.models import Track


def T(title, *artists, ms=None, isrc=None, pid=None):
    return Track(title=title, artists=list(artists), duration_ms=ms, isrc=isrc, platform_id=pid)


def test_clean_title_removes_noise():
    assert clean_title("Let It Be - Remastered 2009") == "let it be"
    assert clean_title("Bohemian Rhapsody (2011 Remaster)") == "bohemian rhapsody"
    assert clean_title("Song Name (feat. Someone)") == "song name"
    assert clean_title("Song Name ft. Someone") == "song name"
    assert clean_title("Titre (Clip Officiel)") == "titre"
    assert clean_title("Déjà vu") == "deja vu"


def test_clean_title_keeps_versions():
    assert "live" in clean_title("Song (Live at Wembley)")
    assert "remix" in clean_title("Song - Remix")


def test_isrc_is_perfect_match():
    assert score(T("A", "X", isrc="FRX1"), T("Completely different", "Y", isrc="frx1")) == 100


def test_same_song_scores_high():
    src = T("Bohemian Rhapsody - Remastered 2011", "Queen", ms=354000)
    cand = T("Bohemian Rhapsody", "Queen", ms=355000)
    assert score(src, cand) >= 95


def test_youtube_style_title():
    src = T("Alors on danse", "Stromae", ms=206000)
    cand = T("Stromae - Alors On Danse (Official Music Video)", ms=208000)
    assert score(src, cand) >= 80


def test_live_version_penalized():
    src = T("Song", "Artist", ms=200000)
    studio = T("Song", "Artist", ms=201000)
    live = T("Song (Live)", "Artist", ms=260000)
    assert score(src, studio) > score(src, live) + 20


def test_wrong_artist_scores_low():
    assert score(T("Hello", "Adele"), T("Hello", "Lionel Richie")) < 75


def test_best_match_threshold():
    src = T("Hello", "Adele", ms=295000)
    good = T("Hello", "Adele", ms=295500, pid="good")
    bad = T("Hello", "Lionel Richie", ms=250000, pid="bad")
    match, s = best_match(src, [bad, good])
    assert match is good and s > 90
    match, _ = best_match(src, [bad])
    assert match is None


def test_build_query():
    assert build_query(T("Song (feat. B) - Radio Edit", "A")) == "A song"
