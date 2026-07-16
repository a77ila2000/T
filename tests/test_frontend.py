from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_status_message_does_not_repeat_barcode_number():
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")

    assert "const barcodeNumber" not in index
    assert "(${barcodeNumber})" not in index
    assert "`${barcodeTypes[barcodeType]}가 생성되었습니다.`" in index


def test_barcode_subtree_opts_out_of_forced_darkening():
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "public" / "style.css").read_text(encoding="utf-8")

    assert '<meta name="color-scheme" content="light dark">' in index
    assert ':root { color-scheme: light dark; }' in index
    assert "style.css?v=20260716-dark-barcode-2" in index
    assert ".barcode-container img" in index
    assert "color-scheme: only light !important" in index
    assert "color-scheme: only light" in css
    assert "forced-color-adjust: none" in css
    assert "background-color: #ffffff !important" in css
    assert "filter: none !important" in css
    assert "@media (prefers-color-scheme: dark)" in css
