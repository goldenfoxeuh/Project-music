import pytest

from app.links import parse_link
from app.models import ProviderError


@pytest.mark.parametrize(
    "url, platform, pid, storefront",
    [
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc", "spotify", "37i9dQZF1DXcBWIGoYBM5M", None),
        ("https://open.spotify.com/intl-fr/playlist/37i9dQZF1DXcBWIGoYBM5M", "spotify", "37i9dQZF1DXcBWIGoYBM5M", None),
        ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", "spotify", "37i9dQZF1DXcBWIGoYBM5M", None),
        ("open.spotify.com/playlist/abc123", "spotify", "abc123", None),
        ("https://www.deezer.com/fr/playlist/908622995", "deezer", "908622995", None),
        ("https://deezer.com/playlist/908622995?utm=x", "deezer", "908622995", None),
        ("https://music.youtube.com/playlist?list=PL4fGSI1pDJn6O1LS0XSdF3RyO0Rq_LDeI", "youtube", "PL4fGSI1pDJn6O1LS0XSdF3RyO0Rq_LDeI", None),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc", "youtube", "PLabc", None),
        ("https://music.youtube.com/browse/VLPLxyz", "youtube", "PLxyz", None),
        ("https://music.apple.com/fr/playlist/top-100-france/pl.6e8cfd81d51042648fa36c9df5236b8d", "apple", "pl.6e8cfd81d51042648fa36c9df5236b8d", "fr"),
        ("https://music.apple.com/us/playlist/my-mix/pl.u-abcdEFGH", "apple", "pl.u-abcdEFGH", "us"),
    ],
)
def test_parse_link(url, platform, pid, storefront):
    ref = parse_link(url)
    assert ref.platform == platform
    assert ref.playlist_id == pid
    if storefront:
        assert ref.storefront == storefront


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/playlist/1",
        "https://open.spotify.com/album/123",
        "https://www.deezer.com/fr/album/123",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.apple.com/fr/album/abc/123",
    ],
)
def test_parse_link_rejects(url):
    with pytest.raises(ProviderError):
        parse_link(url)
