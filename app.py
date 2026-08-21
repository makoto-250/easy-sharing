"""簡易データ共有システム — 16文字の共有キーだけでテキスト/画像/PDFを一時共有する。

仕様書: docs/simple_data_share_system_spec_v1.0.docx
"""
import logging
import secrets
import sqlite3
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    flash,
    get_flashed_messages,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import db
import ratelimit
import storage
import validation

config.validate()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("easy-sharing")

app = Flask(__name__)
# nginx の背後で動くため、X-Forwarded-* を1段だけ信頼する。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    MAX_CONTENT_LENGTH=config.MAX_CONTENT_LENGTH,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=config.IS_PRODUCTION,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=config.RECEIVE_SESSION_SECONDS),
)

TOKYO = ZoneInfo(config.DISPLAY_TIMEZONE)

ERR_KEY_FORMAT = "共有キーは英大文字・英小文字・数字の16文字で入力してください。"
ERR_KEY_TAKEN = "この共有キーは使用できません。別の共有キーを入力してください。"
ERR_NOT_FOUND = "共有キーが正しくないか、受け取り期限が終了しています。"
ERR_RATE_LIMIT = "しばらく時間をおいてから、もう一度お試しください。"
ERR_INTERNAL = "処理を完了できませんでした。時間をおいて再度お試しください。"
ERR_STORAGE_FULL = (
    "現在ファイルの共有を停止しています。テキストの共有はご利用いただけます。"
)


# --- 共通処理 -------------------------------------------------------------


@app.before_request
def _ensure_csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)


@app.after_request
def _security_headers(response):
    """基本的なセキュリティヘッダー（仕様 13.2）。"""
    csp = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if config.IS_PRODUCTION:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.context_processor
def _template_globals():
    return {
        "csrf_token": session.get("csrf", ""),
        "service_name": config.SERVICE_NAME,
        "max_text_length": config.MAX_TEXT_LENGTH,
        "ttl_hours": config.SHARE_TTL_HOURS,
    }


def require_csrf():
    """状態変更処理に CSRF 対策を適用する（仕様 13.2）。"""
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400)


def client_id() -> str:
    return ratelimit.ip_hash(request.remote_addr)


def to_tokyo_text(dt) -> str:
    """「2026年8月21日 23:45まで」の形式（仕様 5.1）。"""
    local = dt.astimezone(TOKYO)
    return f"{local.year}年{local.month}月{local.day}日 {local:%H:%M}まで"


def remaining_text(dt) -> str:
    """残り時間は画面上の参考表示。最終判定はサーバー時刻で行う（仕様 5.1）。"""
    total_minutes = int((dt - db.utcnow()).total_seconds() // 60)
    if total_minutes <= 0:
        return "まもなく期限切れ"
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"残り約 {hours} 時間 {minutes} 分"
    return f"残り約 {minutes} 分"


def clear_share_completion():
    """共有完了画面を離れたら平文の共有キーを保持し続けない（仕様 10.2）。"""
    session.pop("share_done", None)


def clear_receive_session():
    session.pop("receive", None)


def _cleanup_orphan(record):
    """DB 登録前に保存したファイルを回収する。削除に失敗したら記録に残す。

    ここで消し損ねてもファイルは DB のどのレコードからも参照されない孤立状態で、
    cleanup.py の孤立ファイル掃除が最終的に回収する（指摘#6）。
    """
    name = record.get("storage_name")
    if name and not storage.remove(name):
        logger.error("孤立ファイルの回収に失敗しました name=%s", name)


# --- S-01 共有する --------------------------------------------------------


@app.get("/")
def share_form():
    clear_share_completion()
    return render_template("share.html", form={}, errors={})


@app.post("/share")
def share_create():
    require_csrf()
    clear_share_completion()

    who = client_id()
    share_key = validation.normalize_share_key(request.form.get("share_key"))
    data_type = request.form.get("data_type", "text")
    if data_type not in ("text", "file"):
        data_type = "text"
    text_body = request.form.get("text_body", "")

    # 非表示になった入力値は送信対象に含めない（仕様 4.1）。
    form = {"data_type": data_type, "share_key": share_key}
    if data_type == "text":
        form["text_body"] = text_body

    def fail(errors, status=400):
        return render_template("share.html", form=form, errors=errors), status

    with db.session() as conn:
        if not ratelimit.check_share(conn, who):
            return fail({"general": ERR_RATE_LIMIT}, 429)

    if not validation.is_valid_share_key(share_key):
        return fail({"share_key": ERR_KEY_FORMAT})

    record = {
        "id": uuid.uuid4().hex,
        "key_digest": validation.key_digest(share_key),
        "data_type": data_type,
        "text_body": None,
        "storage_name": None,
        "original_name": None,
        "mime_type": None,
        "file_size": None,
        "status": "active",
    }
    upload = None

    if data_type == "text":
        if not text_body.strip():
            return fail({"text_body": "共有するテキストを入力してください。"})
        if len(text_body) > config.MAX_TEXT_LENGTH:
            return fail(
                {
                    "text_body": "テキストは{:,}文字以内で入力してください。".format(
                        config.MAX_TEXT_LENGTH
                    )
                }
            )
        record["text_body"] = text_body
        preview = {
            "kind": "text",
            "text": text_body[:100],
            "truncated": len(text_body) > 100,
        }
    else:
        # 複数選択または複数ファイル送信を拒否する（受け入れ条件 V-04）。
        uploads = [f for f in request.files.getlist("file") if f and f.filename]
        if len(uploads) > 1:
            return fail({"file": "共有できるファイルは1件だけです。"})
        if not uploads:
            return fail({"file": "共有するファイルを選択してください。"})
        upload = uploads[0]

        try:
            mime_type, size, _head = validation.validate_upload(upload, upload.mimetype)
        except validation.FileRejected as exc:
            return fail({exc.field: exc.message})

        # 容量の確定判定は下の書き込みロック内で行う（すり抜け対策 / 指摘#5）。
        record["storage_name"] = storage.new_storage_name()
        record["original_name"] = validation.sanitize_original_name(upload.filename)
        record["mime_type"] = mime_type
        record["file_size"] = size
        preview = {
            "kind": "file",
            "name": record["original_name"],
            "format": validation.format_label(mime_type),
            "size": validation.format_size(size),
        }

    created_at = db.utcnow()
    expires_at = db.expires_at_for(created_at)
    record["created_at"] = db.to_db(created_at)
    record["expires_at"] = db.to_db(expires_at)

    # ファイル保存と DB 保存のどちらかが失敗したら残りを回収する（仕様 11.1）。
    if upload is not None:
        try:
            storage.save(upload, record["storage_name"])
        except OSError:
            logger.exception("ファイルの保存に失敗しました id=%s", record["id"])
            return fail({"general": ERR_INTERNAL}, 500)

    # レート制限・容量制限の確認と、レコード挿入・記録を1つの書き込みロック内で
    # まとめて行う。上の早期チェックは高速な門前払い用で、ここが確定判定になる
    # （2ワーカーが同時に制限すり抜けするのを防ぐ / 指摘#5）。
    try:
        with db.immediate() as conn:
            if not ratelimit.check_share(conn, who):
                _cleanup_orphan(record)
                return fail({"general": ERR_RATE_LIMIT}, 429)
            if upload is not None:
                used = db.total_stored_bytes(conn)
                if used + record["file_size"] > (
                    config.TOTAL_STORAGE_LIMIT * config.STORAGE_WARN_RATIO
                ):
                    _cleanup_orphan(record)
                    logger.error(
                        "保存容量が上限の%.0f%%に達しました used=%d limit=%d",
                        config.STORAGE_WARN_RATIO * 100,
                        used,
                        config.TOTAL_STORAGE_LIMIT,
                    )
                    return fail({"general": ERR_STORAGE_FULL}, 507)
            db.insert_share(conn, record)
            ratelimit.record_share(conn, who)
    except sqlite3.IntegrityError:
        # 手入力キーの重複はエラーとして返す（仕様 4.3）。
        _cleanup_orphan(record)
        return fail({"share_key": ERR_KEY_TAKEN})
    except sqlite3.Error:
        _cleanup_orphan(record)
        logger.exception("共有レコードの保存に失敗しました id=%s", record["id"])
        return fail({"general": ERR_INTERNAL}, 500)

    logger.info(
        "共有を作成しました id=%s type=%s size=%s expires=%s",
        record["id"],
        record["data_type"],
        record["file_size"],
        record["expires_at"],
    )

    # POST/Redirect/GET で二重送信を防ぐ（仕様 11.1）。
    session["share_done"] = {
        "share_key": share_key,
        "expires_at": record["expires_at"],
        "preview": preview,
    }
    return redirect(url_for("share_done"))


@app.get("/share/done")
def share_done():
    done = session.get("share_done")
    if not done:
        return redirect(url_for("share_form"))
    expires_at = db.from_db(done["expires_at"])
    response = app.make_response(
        render_template(
            "share_done.html",
            share_key=done["share_key"],
            preview=done["preview"],
            expires_text=to_tokyo_text(expires_at),
            remaining=remaining_text(expires_at),
        )
    )
    # 平文の共有キーを表示する画面はキャッシュに残さない。
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# --- S-03 受け取る --------------------------------------------------------


@app.get("/receive")
def receive_form():
    clear_share_completion()
    # 受け取りフォームに戻ってきた時点で、過去の受け取り認可を破棄する。
    # これがないと、別のキーで検索に失敗しても直接 /receive/result を開いて
    # 前回のデータを再表示できてしまう（共用端末での意図しない閲覧）。
    clear_receive_session()
    return render_template("receive.html", error=None, share_key="")


@app.post("/receive")
def receive_lookup():
    require_csrf()
    clear_share_completion()
    # 新しい検索を始める前に、前回の受け取り認可を必ず捨てる。
    # 検索が失敗して終わっても古い認可が残らないようにする。
    clear_receive_session()

    who = client_id()
    share_key = validation.normalize_share_key(request.form.get("share_key"))

    def fail(message, status=400):
        page = render_template("receive.html", error=message, share_key=share_key)
        return page, status

    with db.session() as conn:
        if not ratelimit.check_receive(conn, who):
            return fail(ERR_RATE_LIMIT, 429)

    if not validation.is_valid_share_key(share_key):
        # 形式不正も総当たりの一部なので失敗として数える。
        with db.session() as conn:
            ratelimit.record_receive_failure(conn, who)
        return fail(ERR_KEY_FORMAT)

    with db.session() as conn:
        row = db.find_active_by_digest(conn, validation.key_digest(share_key))
        if row is None:
            # 未登録と期限切れを区別せず、同じ経路・同じ文言で返す（仕様 6章）。
            ratelimit.record_receive_failure(conn, who)
            return fail(ERR_NOT_FOUND, 404)
        share_id = row["id"]

    # 共有キーは保持せず、内部IDだけを短時間有効なセッションへ入れる（仕様 11.2）。
    session.permanent = True
    session["receive"] = {"id": share_id, "issued_at": db.to_db(db.utcnow())}
    return redirect(url_for("receive_result"))


def _current_receive_row(conn):
    """受け取りセッションの妥当性と、期限・状態をその都度再確認する（仕様 7.1）。"""
    data = session.get("receive")
    if not data:
        return None
    issued_at = db.from_db(data["issued_at"])
    if db.utcnow() - issued_at > timedelta(seconds=config.RECEIVE_SESSION_SECONDS):
        return None
    return db.find_active_by_id(conn, data["id"])


@app.get("/receive/result")
def receive_result():
    with db.session() as conn:
        row = _current_receive_row(conn)
    if row is None:
        clear_receive_session()
        page = render_template("receive.html", error=ERR_NOT_FOUND, share_key="")
        return page, 404

    expires_at = db.from_db(row["expires_at"])
    context = {
        "data_type": row["data_type"],
        "expires_text": to_tokyo_text(expires_at),
        "remaining": remaining_text(expires_at),
        "text_body": row["text_body"],
        "file_name": row["original_name"],
        "file_format": (
            validation.format_label(row["mime_type"]) if row["mime_type"] else ""
        ),
        "file_size": (
            validation.format_size(row["file_size"]) if row["file_size"] else ""
        ),
    }
    response = app.make_response(render_template("receive_result.html", **context))
    # 結果画面はキャッシュに残さない（仕様 7.1）。
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/receive/download")
def receive_download():
    """共有キーは URL に含めない。受け取りセッションだけで認可する（仕様 11.3）。"""
    who = client_id()
    with db.session() as conn:
        if not ratelimit.check_download(conn, who):
            abort(429)
        row = _current_receive_row(conn)
        if row is None or row["data_type"] != "file":
            clear_receive_session()
            abort(404)
        ratelimit.record_download(conn, who)
        storage_name = row["storage_name"]
        mime_type = row["mime_type"]
        original_name = row["original_name"]
        share_id = row["id"]

    try:
        handle = storage.open_for_read(storage_name)
    except OSError:
        logger.exception("保存ファイルを読み出せませんでした id=%s", share_id)
        abort(500)

    # 同一ドメイン上で直接実行・表示させない（仕様 7.3）。
    response = send_file(
        handle,
        mimetype=mime_type,
        as_attachment=True,
        download_name=original_name,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# --- S-05 任意削除 --------------------------------------------------------


@app.post("/receive/delete")
def receive_delete():
    require_csrf()
    with db.session() as conn:
        row = _current_receive_row(conn)
        if row is None:
            clear_receive_session()
            page = render_template("receive.html", error=ERR_NOT_FOUND, share_key="")
            return page, 404
        share_id = row["id"]
        storage_name = row["storage_name"]
        # 先に受け取り不可（delete_pending）へ遷移させてから物理削除する。
        # この順にすると、ファイル削除後にDBが落ちても「active なのに実体がない」
        # 状態にはならず、cleanup.py が確実に後始末できる（仕様 8.3）。
        db.mark_delete_pending(conn, share_id)

    removed = storage.remove(storage_name)
    if removed:
        with db.session() as conn:
            db.delete_share(conn, share_id)

    logger.info("任意削除を実行しました id=%s file_removed=%s", share_id, removed)
    clear_receive_session()
    flash("deleted")
    return redirect(url_for("deleted"))


@app.get("/deleted")
def deleted():
    if not get_flashed_messages():
        return redirect(url_for("share_form"))
    return render_template("deleted.html")


# --- エラーハンドラ -------------------------------------------------------


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_exc):
    errors = {"file": "ファイルサイズは 20 MiB 以下にしてください。"}
    return render_template("share.html", form={"data_type": "file"}, errors=errors), 413


@app.errorhandler(400)
def _bad_request(_exc):
    return render_template("error.html", message=ERR_INTERNAL), 400


@app.errorhandler(404)
def _not_found(_exc):
    return render_template("error.html", message="ページが見つかりません。"), 404


@app.errorhandler(429)
def _too_many(_exc):
    return render_template("error.html", message=ERR_RATE_LIMIT), 429


@app.errorhandler(500)
def _server_error(_exc):
    return render_template("error.html", message=ERR_INTERNAL), 500


db.init_db()
storage.ensure_storage_dir()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5040, debug=True)
