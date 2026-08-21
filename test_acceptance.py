"""受け入れ条件・テスト項目（仕様書 16章）をそのまま検証する。

実行: .venv/Scripts/python.exe -m pytest test_acceptance.py -q
"""
import io
import os
import re
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

# アプリを読み込む前にテスト専用の保存先と秘密鍵を設定する。
_TMP = Path(tempfile.mkdtemp(prefix="easy-sharing-test-"))
os.environ["DATABASE_PATH"] = str(_TMP / "test.db")
os.environ["UPLOAD_STORAGE_PATH"] = str(_TMP / "uploads")
os.environ["KEY_HMAC_SECRET"] = "test-hmac-secret-value-32-characters-long"
os.environ["SECRET_KEY"] = "test-flask-secret"
os.environ["FLASK_ENV"] = "development"

import app as app_module  # noqa: E402
import db  # noqa: E402
import storage  # noqa: E402
import validation  # noqa: E402

VALID_KEY = "AbCdEfGh12345678"

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
SAMPLES = {
    "sample.pdf": (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer\n%%EOF\n", "application/pdf"),
    "sample.gif": (b"GIF89a" + b"\x01\x00\x01\x00\x00\xff\x00,", "image/gif"),
    "sample.jpg": (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32, "image/jpeg"),
    "sample.png": (PNG, "image/png"),
    "sample.webp": (b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24, "image/webp"),
}


@pytest.fixture
def client():
    with db.session() as conn:
        conn.execute("DELETE FROM shares")
        conn.execute("DELETE FROM rate_events")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.get("/")  # CSRF トークンをセッションに載せる
        yield c


def csrf(client):
    with client.session_transaction() as sess:
        return sess["csrf"]


def share_text(client, key=VALID_KEY, text="こんにちは", follow=True):
    return client.post(
        "/share",
        data={
            "csrf_token": csrf(client),
            "data_type": "text",
            "text_body": text,
            "share_key": key,
        },
        follow_redirects=follow,
    )


def share_file(client, filename, content, mime, key=VALID_KEY, field="file"):
    return client.post(
        "/share",
        data={
            "csrf_token": csrf(client),
            "data_type": "file",
            field: (io.BytesIO(content), filename, mime),
            "share_key": key,
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def receive(client, key=VALID_KEY):
    return client.post(
        "/receive",
        data={"csrf_token": csrf(client), "share_key": key},
        follow_redirects=True,
    )


def body(response):
    return response.get_data(as_text=True)


# --- F: 機能 --------------------------------------------------------------


def test_f01_text_share_shows_preview_key_and_expiry():
    """F-01 完了画面に先頭100文字、キー、期限が表示される。"""
    with app_module.app.test_client() as client:
        client.get("/")
        long_text = "あ" * 250
        page = body(share_text(client, text=long_text))
        assert "あ" * 100 in page
        assert "あ" * 101 not in page  # 100文字で切って省略記号を付ける
        assert "…" in page
        assert VALID_KEY in page
        assert re.search(r"\d+年\d+月\d+日 \d{2}:\d{2}まで", page)


@pytest.mark.parametrize("filename", sorted(SAMPLES))
def test_f02_allowed_file_types_are_accepted(client, filename):
    """F-02 PDF/GIF/JPEG/PNG/WebP をそれぞれ1件ずつ共有できる。"""
    content, mime = SAMPLES[filename]
    page = body(share_file(client, filename, content, mime))
    assert "共有しました" in page
    assert filename in page


def test_f04_multiple_receives_within_expiry(client):
    """F-04 期限内・未削除であれば同じキーで複数回受け取れる。"""
    share_text(client, text="繰り返し受け取れる")
    for _ in range(3):
        page = body(receive(client))
        assert "繰り返し受け取れる" in page


def test_f05_manual_delete_removes_file_and_record(client):
    """F-05 削除後は同じキーで受け取れず、ファイル本体とレコードが消える。"""
    share_file(client, "sample.png", PNG, "image/png")
    receive(client)
    with db.session() as conn:
        row = conn.execute("SELECT storage_name FROM shares").fetchone()
    file_path = storage.path_for(row["storage_name"])
    assert file_path.exists()

    page = body(
        client.post(
            "/receive/delete",
            data={"csrf_token": csrf(client)},
            follow_redirects=True,
        )
    )
    assert "データを削除しました" in page
    assert not file_path.exists()
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM shares").fetchone()["c"] == 0
    assert app_module.ERR_NOT_FOUND in body(receive(client))


# --- K: 共有キー ----------------------------------------------------------


def test_k01_generated_keys_are_16_alnum_from_secure_rng():
    """K-01 16文字で A-Z/a-z/0-9 のみ、暗号学的乱数で生成される。"""
    keys = {validation.generate_share_key() for _ in range(200)}
    assert len(keys) == 200  # 衝突しない
    for key in keys:
        assert re.fullmatch(r"[A-Za-z0-9]{16}", key)
    # secrets モジュール（OS の CSPRNG）を使っている
    assert "secrets" in validation.generate_share_key.__module__ or True
    import inspect

    assert "secrets.choice" in inspect.getsource(validation.generate_share_key)


def test_k02_keys_are_case_sensitive(client):
    """K-02 大文字小文字が異なるキーは別のキーとして扱われる。"""
    share_text(client, key="AbCdEfGh12345678", text="オリジナル")
    # 大文字小文字だけ違うキーは別物なので保存できる
    page = body(share_text(client, key="abcdefgh12345678", text="別データ"))
    assert "共有しました" in page
    assert "オリジナル" in body(receive(client, "AbCdEfGh12345678"))
    assert "別データ" in body(receive(client, "abcdefgh12345678"))


def test_k03_duplicate_manual_key_is_rejected(client):
    """K-03 既存キーと重複した場合、指定メッセージを表示して保存しない。"""
    share_text(client, text="先に共有した内容")
    page = body(share_text(client, text="あとから共有した内容"))
    assert app_module.ERR_KEY_TAKEN in page
    assert "先に共有した内容" in body(receive(client))


# --- V: 入力検証 ----------------------------------------------------------


def test_v01_empty_inputs_are_rejected(client):
    """V-01 空のテキスト、未選択ファイル、空キーを拒否する。"""
    assert "共有するテキストを入力してください。" in body(share_text(client, text="   "))
    assert app_module.ERR_KEY_FORMAT in body(share_text(client, key=""))
    page = body(
        client.post(
            "/share",
            data={"csrf_token": csrf(client), "data_type": "file", "share_key": VALID_KEY},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
    )
    assert "共有するファイルを選択してください。" in page


def test_v02_size_boundary(client):
    """V-02 20 MiB 以下を許可し、1 byte でも超えたファイルを拒否する。"""
    import config

    header = PNG[:33]
    exact = header + b"\x00" * (config.MAX_FILE_BYTES - len(header))
    assert len(exact) == config.MAX_FILE_BYTES
    assert "共有しました" in body(share_file(client, "big.png", exact, "image/png"))

    over = exact + b"\x00"
    page = body(share_file(client, "over.png", over, "image/png", key="ZzYyXxWw87654321"))
    assert "20 MiB 以下" in page


@pytest.mark.parametrize(
    "filename,content,mime",
    [
        # PHP を .jpg へ改名 → シグネチャ不一致で拒否
        ("evil.jpg", b"<?php system($_GET['c']); ?>", "image/jpeg"),
        # HTML / SVG / ZIP / 実行形式は拡張子を変えても拒否
        ("evil.png", b"<html><script>alert(1)</script></html>", "image/png"),
        ("evil.png", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/png"),
        ("evil.pdf", b"PK\x03\x04zipcontent", "application/pdf"),
        ("evil.png", b"MZ\x90\x00\x03", "image/png"),
        # 許可拡張子だが中身が別形式
        ("mismatch.png", SAMPLES["sample.gif"][0], "image/png"),
        # 未対応拡張子
        ("doc.txt", b"plain text", "text/plain"),
        ("archive.zip", b"PK\x03\x04", "application/zip"),
    ],
)
def test_v03_disguised_files_are_rejected(client, filename, content, mime):
    """V-03 拡張子・MIME・シグネチャのいずれかが不一致なら拒否する。"""
    page = body(share_file(client, filename, content, mime))
    assert "このファイル形式には対応していません。" in page
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM shares").fetchone()["c"] == 0


def test_v04_multiple_files_are_rejected(client):
    """V-04 複数選択または複数ファイル送信を拒否する。"""
    page = body(
        client.post(
            "/share",
            data={
                "csrf_token": csrf(client),
                "data_type": "file",
                "file": [
                    (io.BytesIO(PNG), "a.png", "image/png"),
                    (io.BytesIO(PNG), "b.png", "image/png"),
                ],
                "share_key": VALID_KEY,
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
    )
    assert "ファイルは1件だけです。" in page
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM shares").fetchone()["c"] == 0


def test_text_length_limit(client):
    """100,000文字を超えるテキストを拒否する（仕様 12章）。"""
    import config

    ok = "a" * config.MAX_TEXT_LENGTH
    assert "共有しました" in body(share_text(client, text=ok))
    over = "a" * (config.MAX_TEXT_LENGTH + 1)
    page = body(share_text(client, text=over, key="ZzYyXxWw87654321"))
    assert "100,000文字以内" in page


# --- E: 有効期限 ----------------------------------------------------------


def _shift_expiry(delta):
    with db.session() as conn:
        row = conn.execute("SELECT id, expires_at FROM shares").fetchone()
        conn.execute(
            "UPDATE shares SET expires_at = ? WHERE id = ?",
            (db.to_db(db.from_db(row["expires_at"]) + delta), row["id"]),
        )


def test_e01_just_before_expiry_is_receivable(client):
    """E-01 expires_at 未満では受け取れる。"""
    share_text(client, text="期限直前でも読める")
    _shift_expiry(timedelta(hours=-12) + timedelta(seconds=5))
    assert "期限直前でも読める" in body(receive(client))


def test_e02_expired_is_not_receivable_even_if_file_remains(client):
    """E-02 期限到達後は物理ファイルが残っていても受け取れない。"""
    share_file(client, "sample.png", PNG, "image/png")
    with db.session() as conn:
        row = conn.execute("SELECT storage_name FROM shares").fetchone()
    _shift_expiry(timedelta(hours=-13))
    assert storage.path_for(row["storage_name"]).exists()
    assert app_module.ERR_NOT_FOUND in body(receive(client))


def test_e03_cleanup_deletes_expired_and_retries_failures(client):
    """E-03 定期削除で期限切れデータが削除され、失敗は再試行対象になる。"""
    import cleanup

    share_file(client, "sample.png", PNG, "image/png")
    with db.session() as conn:
        row = conn.execute("SELECT id, storage_name FROM shares").fetchone()
    path = storage.path_for(row["storage_name"])
    _shift_expiry(timedelta(hours=-13))

    assert cleanup.run() == 0
    assert not path.exists()
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM shares").fetchone()["c"] == 0

    # 削除待ち（status=delete_pending）は期限内でも再処理の対象になる。
    share_text(client, text="削除待ちの再処理", key="PpQqRrSs11223344")
    with db.session() as conn:
        pending = conn.execute("SELECT id FROM shares").fetchone()["id"]
        db.mark_delete_pending(conn, pending)
    assert cleanup.run() == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM shares").fetchone()["c"] == 0


def test_delete_pending_is_not_receivable(client):
    """削除待ち状態のデータは受け取れない（仕様 8.3）。"""
    share_text(client, text="削除待ち")
    with db.session() as conn:
        share_id = conn.execute("SELECT id FROM shares").fetchone()["id"]
        db.mark_delete_pending(conn, share_id)
    assert app_module.ERR_NOT_FOUND in body(receive(client))


# --- S: セキュリティ ------------------------------------------------------


def test_s01_plaintext_key_is_never_stored_or_exposed(client):
    """S-01 共有キー平文が DB、URL、ログに残らない。"""
    response = share_text(client, text="キーは漏れない", follow=False)
    # リダイレクト先の URL に共有キーを含めない
    assert VALID_KEY not in response.headers["Location"]

    # DB のどのカラムにも平文キーが入っていない
    raw = sqlite3.connect(os.environ["DATABASE_PATH"])
    dump = "\n".join(raw.iterdump())
    raw.close()
    assert VALID_KEY not in dump
    # 保存されているのは HMAC-SHA-256 のダイジェスト
    with db.session() as conn:
        stored = conn.execute("SELECT key_digest FROM shares").fetchone()["key_digest"]
    assert stored == validation.key_digest(VALID_KEY)
    assert re.fullmatch(r"[0-9a-f]{64}", stored)

    # 受け取り後の URL にも含まれない
    lookup = client.post(
        "/receive", data={"csrf_token": csrf(client), "share_key": VALID_KEY}
    )
    assert VALID_KEY not in lookup.headers["Location"]


def test_s02_xss_payload_is_escaped(client):
    """S-02 HTML/スクリプトを含むテキストが実行されず、文字として表示される。"""
    payload = "<script>alert('xss')</script><img src=x onerror=alert(1)>"
    share_text(client, text=payload)
    page = body(receive(client))
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page
    assert "onerror=alert(1)>" not in page


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd.png", "passwd.png"),
        ("..\\..\\windows\\system32\\evil.png", "evil.png"),
        ("pas\r\nswd.png", "passwd.png"),
        ("null\x00byte.png", "nullbyte.png"),
        ("\x1b[31mescape.png", "[31mescape.png"),
        ("   .   ", "file"),
        ("", "file"),
    ],
)
def test_s02b_filename_sanitizer(raw, expected):
    """元ファイル名のパス区切り・CR/LF・NULL・制御文字を除去する（仕様 13.1）。"""
    cleaned = validation.sanitize_original_name(raw)
    assert cleaned == expected
    assert not any(c in cleaned for c in "/\\\r\n\x00")


def test_s02c_filename_is_escaped_on_screen(client):
    """元ファイル名は表示時に HTML エスケープする（仕様 13.2）。"""
    # 引用符とパス区切りはマルチパート転送側で壊れるため、それ以外の HTML を使う
    evil = "<img src=x onerror=alert(1)>.png"
    share_file(client, evil, PNG, "image/png")
    with db.session() as conn:
        stored = conn.execute("SELECT original_name FROM shares").fetchone()
    assert stored["original_name"] == evil  # 保存時は素通し、表示時にエスケープする

    page = body(receive(client))
    assert evil not in page
    assert "&lt;img src=x onerror=alert(1)&gt;.png" in page


def test_s03_storage_is_outside_web_root_and_name_is_random(client):
    """S-03 保存ファイルのパスを推測しても Web から直接取得できない。"""
    import config

    share_file(client, "sample.png", PNG, "image/png")
    with db.session() as conn:
        row = conn.execute("SELECT storage_name, original_name FROM shares").fetchone()

    # 内部ファイル名は 64 桁のランダム hex。元ファイル名や共有キーから作らない。
    assert re.fullmatch(r"[0-9a-f]{64}", row["storage_name"])
    assert row["original_name"].rsplit(".", 1)[0] not in row["storage_name"]
    assert validation.key_digest(VALID_KEY) != row["storage_name"]

    # 保存先が static 配下（＝Web から配信される領域）ではない
    static_dir = Path(app_module.app.static_folder).resolve()
    assert static_dir not in config.UPLOAD_STORAGE_PATH.parents
    assert config.UPLOAD_STORAGE_PATH != static_dir

    # 保存名を URL に当てても 404
    assert client.get("/static/" + row["storage_name"]).status_code == 404
    assert client.get("/" + row["storage_name"]).status_code == 404


def test_s04_receive_rate_limit(client):
    """S-04 規定回数を超えるキー試行が制限される。"""
    import config

    for _ in range(config.RATE_RECEIVE_FAIL_PER_MIN):
        assert app_module.ERR_NOT_FOUND in body(receive(client, "ZzZzZzZz99999999"))
    blocked = receive(client, "ZzZzZzZz99999999")
    assert blocked.status_code == 429
    assert app_module.ERR_RATE_LIMIT in body(blocked)

    # 制限中は正しいキーでも受け付けない（総当たり中の巻き添えを許容する安全側の挙動）
    share_text(client, text="制限中")
    assert app_module.ERR_RATE_LIMIT in body(receive(client, VALID_KEY))


def test_s04b_share_rate_limit(client):
    """共有処理は同一IPから10回/10分で制限される（仕様 13.3）。"""
    import config

    for i in range(config.RATE_SHARE_PER_10MIN):
        key = "Aa{:014d}".format(i)
        assert "共有しました" in body(share_text(client, key=key, text="x"))
    over = share_text(client, key="Bb000000000000zz", text="x")
    assert over.status_code == 429
    assert app_module.ERR_RATE_LIMIT in body(over)


def test_s05_expiry_is_rechecked_on_download_and_delete(client):
    """S-05 結果表示後に期限が切れたら、ダウンロードと削除が拒否される。"""
    share_file(client, "sample.png", PNG, "image/png")
    assert "ダウンロード" in body(receive(client))

    # 結果画面を開いたあとに期限切れになる
    _shift_expiry(timedelta(hours=-13))

    assert client.get("/receive/download").status_code == 404
    deleted = client.post(
        "/receive/delete", data={"csrf_token": csrf(client)}, follow_redirects=True
    )
    assert deleted.status_code == 404
    assert app_module.ERR_NOT_FOUND in body(deleted)


def test_csrf_is_required_for_state_changes(client):
    """状態変更処理に CSRF 対策が効いている（仕様 13.2）。"""
    assert client.post("/share", data={"data_type": "text"}).status_code == 400
    assert client.post("/receive", data={"share_key": VALID_KEY}).status_code == 400
    assert client.post("/receive/delete", data={}).status_code == 400
    assert (
        client.post(
            "/share", data={"csrf_token": "wrong", "data_type": "text"}
        ).status_code
        == 400
    )


def test_security_headers_and_no_store_on_result(client):
    """結果画面の no-store と基本セキュリティヘッダー（仕様 7.1 / 13.2）。"""
    share_text(client, text="ヘッダー確認")
    result = receive(client)
    assert "no-store" in result.headers["Cache-Control"]
    for header in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "Referrer-Policy",
    ):
        assert header in result.headers
    assert result.headers["Referrer-Policy"] == "no-referrer"
    assert result.headers["X-Content-Type-Options"] == "nosniff"


def test_download_uses_attachment_and_nosniff(client):
    """ダウンロードは attachment + nosniff で返す（仕様 7.3）。"""
    share_file(client, "sample.pdf", *SAMPLES["sample.pdf"][::-1][::-1])
    receive(client)
    response = client.get("/receive/download")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response.headers["Cache-Control"]
    assert response.get_data() == SAMPLES["sample.pdf"][0]


def test_share_key_is_only_shown_once(client):
    """共有キーは完了画面でのみ再表示できる（仕様 5.1）。"""
    assert VALID_KEY in body(share_text(client))
    # 完了画面を離れると保持しない
    client.get("/")
    assert client.get("/share/done").status_code == 302


def test_receive_session_expires(client):
    """受け取りセッションは短時間で失効する（仕様 11.2）。"""
    import config

    share_text(client, text="セッション失効")
    receive(client)
    with client.session_transaction() as sess:
        stale = db.utcnow() - timedelta(seconds=config.RECEIVE_SESSION_SECONDS + 60)
        sess["receive"]["issued_at"] = db.to_db(stale)
    assert client.get("/receive/result").status_code == 404
    assert client.get("/receive/download").status_code == 404
