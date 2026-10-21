from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
import smtplib
import ssl
from unittest.mock import Mock

import pytest

from chatbot import report_email as mail


PDF = b"%PDF-1.4\nreport bytes\n%%EOF"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("socket.create_connection", Mock(side_effect=AssertionError("No network")))


@pytest.fixture
def smtp(monkeypatch):
    client = Mock()
    client.send_message.return_value = {}
    factory = Mock(return_value=client)
    secure_factory = Mock(return_value=client)
    monkeypatch.setattr(mail.smtplib, "SMTP", factory)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", secure_factory)
    return client, factory, secure_factory


@pytest.fixture
def settings():
    return mail.EmailSettings(host="smtp.example.com", user="sender", password="SECRET",
                              from_address="sender@example.com")


@pytest.mark.parametrize("address", ["a@example.com", "First.Last+tag@EXAMPLE.COM", "o'neil@example.com"])
def test_normalize_valid(address):
    assert mail.normalize_email(" " + address + " ") == address.split("@")[0] + "@example.com"


@pytest.mark.parametrize("address", [None, "", "x", "x@localhost", "a@-bad.com", "a@bad-.com",
    "a@bad_label.com", "a@a..com", "a@example.com.", ".a@example.com", "a.@example.com",
    "a..b@example.com", '"a b"@example.com', "A <a@example.com>", "a@example.com,b@example.com",
    "a@example.com;b@example.com", "a@example.com\r\nBcc: b@example.com", "a@example.com\n",
    "ü@example.com", "a@éxample.com", "a b@example.com", "a@" + "x" * 64 + ".com",
    "a" * 65 + "@example.com", "a@exam\x00ple.com", "a@[127.0.0.1]"])
def test_normalize_rejects_unsafe(address):
    with pytest.raises(mail.EmailError):
        mail.normalize_email(address)


def test_mailbox_length_boundaries():
    local = "x" * 64
    domain = ".".join(["a" * 63, "b" * 63, "c" * 61])
    assert len(mail.normalize_email(local + "@" + domain)) == 254
    with pytest.raises(mail.EmailError):
        mail.normalize_email(local + "@" + domain + "c")


def test_settings_env_and_secret_repr(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("CHATBOT_SMTP_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(mail, "ROOT", tmp_path)
    assert not mail.EmailSettings.from_env().configured
    (tmp_path / ".env").write_text('CHATBOT_SMTP_HOST=smtp.example.com\nCHATBOT_SMTP_USER=user\n'
        'CHATBOT_SMTP_PASSWORD=secret-from-env\nCHATBOT_SMTP_FROM=sender@example.com\n')
    monkeypatch.setenv("CHATBOT_SMTP_HOST", "override.example.com")
    # dotenv mutates the environment, so register keys for fixture cleanup first.
    for key in ("USER", "PASSWORD", "FROM"):
        monkeypatch.setenv("CHATBOT_SMTP_" + key, "")
        monkeypatch.delenv("CHATBOT_SMTP_" + key)
    settings = mail.EmailSettings.from_env()
    assert settings.configured and settings.host == "override.example.com"
    assert settings.port == 587 and settings.tls_mode == "starttls"
    assert settings.password == "secret-from-env" and settings.password not in repr(settings)
    monkeypatch.setenv("CHATBOT_SMTP_PORT", "465")
    monkeypatch.setenv("CHATBOT_SMTP_TLS_MODE", "ssl")
    assert mail.EmailSettings.from_env().port == 465
    assert mail.EmailSettings.from_env().tls_mode == "ssl"


@pytest.mark.parametrize("key,value", [("PORT", "oops"), ("PORT", "0"), ("PORT", "65536"),
    ("TLS_MODE", "none"), ("HOST", "host\r\nsecret"), ("FROM", "bad address"), ("USER", "user")])
def test_invalid_env_safe_errors(monkeypatch, tmp_path, key, value):
    monkeypatch.setattr(mail, "ROOT", tmp_path)
    for name in list(os.environ):
        if name.startswith("CHATBOT_SMTP_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CHATBOT_SMTP_" + key, value)
    with pytest.raises(mail.EmailError, match="Invalid SMTP configuration") as error:
        mail.EmailSettings.from_env()
    assert value not in str(error.value)


def test_starttls_sequence_and_private_message(smtp, settings):
    client, factory, secure_factory = smtp
    assert mail.send_report_pdf(PDF, "Quarterly report", "A@EXAMPLE.COM", settings) is None
    factory.assert_called_once_with(settings.host, 587, timeout=mail.SMTP_TIMEOUT)
    secure_factory.assert_not_called()
    assert [call[0] for call in client.method_calls] == [
        "ehlo", "starttls", "ehlo", "login", "send_message", "quit", "close"]
    context = client.starttls.call_args.kwargs["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    message = client.send_message.call_args.args[0]
    assert str(message["Subject"]) == "Quarterly report"
    assert message["To"] == "A@example.com"
    assert client.send_message.call_args.kwargs == {"from_addr": "sender@example.com", "to_addrs": ["A@example.com"]}
    parts = list(message.iter_parts())
    assert len(parts) == 2 and parts[0].get_content_type() == "text/plain"
    assert parts[0].get_content().strip() == "Your requested PDF report is attached."
    assert parts[1].get_content_type() == "application/pdf" and parts[1].get_payload(decode=True) == PDF
    assert parts[1].get_filename() == "Quarterly-report.pdf"
    serialized = message.as_string()
    assert "SECRET" not in serialized and "SELECT" not in serialized and "question" not in serialized


def test_ssl_and_no_auth(smtp, settings):
    client, factory, secure_factory = smtp
    mail.send_report_pdf(PDF, "Report", "a@example.com", replace(settings, tls_mode="ssl", user="", password=""))
    factory.assert_not_called()
    assert secure_factory.call_args.kwargs["context"].check_hostname
    client.starttls.assert_not_called()
    client.login.assert_not_called()
    client.ehlo.assert_called_once()


def test_title_sanitized_and_bounded(smtp, settings):
    client, _, _ = smtp
    mail.send_report_pdf(PDF, "../../evil\r\nBcc: bad@example.com " + "é" * 500, "a@example.com", settings)
    message = client.send_message.call_args.args[0]
    assert len(str(message["Subject"])) <= 160 and message["Bcc"] is None
    filename = next(message.iter_attachments()).get_filename()
    assert "/" not in filename and ".." not in filename and len(filename) <= 84
    message.as_bytes()


@pytest.mark.parametrize("data", [b"", b"not a pdf", bytearray(PDF), "%PDF", b"%PDF" + b"x" * mail.MAX_PDF_BYTES])
def test_invalid_pdf_never_connects(smtp, settings, data):
    with pytest.raises(mail.EmailError):
        mail.send_report_pdf(data, "Report", "a@example.com", settings)
    smtp[1].assert_not_called()
    smtp[2].assert_not_called()


def test_pdf_exact_limit_accepted(smtp, settings):
    mail.send_report_pdf(b"%PDF" + b"x" * (mail.MAX_PDF_BYTES - 4), "Report", "a@example.com", settings)
    smtp[0].send_message.assert_called_once()


@pytest.mark.parametrize("change", [{"host": ""}, {"from_address": ""}, {"port": 0},
    {"tls_mode": "plain"}, {"user": "", "password": "SECRET"}])
def test_unconfigured_never_connects(smtp, settings, change):
    with pytest.raises(mail.EmailError):
        mail.send_report_pdf(PDF, "Report", "a@example.com", replace(settings, **change))
    smtp[1].assert_not_called()


@pytest.mark.parametrize("failure", [smtplib.SMTPServerDisconnected("SECRET"), TimeoutError("SECRET"),
    ConnectionResetError("SECRET"), OSError("SECRET")])
def test_send_interrupted_is_uncertain_without_retry(smtp, settings, failure):
    client, factory, _ = smtp
    client.send_message.side_effect = failure
    with pytest.raises(mail.DeliveryUncertain) as error:
        mail.send_report_pdf(PDF, "Report", "a@example.com", settings)
    assert "SECRET" not in str(error.value) and error.value.__suppress_context__
    client.send_message.assert_called_once()
    factory.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("stage", ["ehlo", "starttls", "login"])
def test_setup_disconnect_is_definite_safe_error(smtp, settings, stage):
    setattr(smtp[0], stage, Mock(side_effect=smtplib.SMTPServerDisconnected("SECRET")))
    with pytest.raises(mail.EmailError) as error:
        mail.send_report_pdf(PDF, "Report", "a@example.com", settings)
    assert not isinstance(error.value, mail.DeliveryUncertain) and "SECRET" not in str(error.value)
    smtp[0].send_message.assert_not_called()


@pytest.mark.parametrize("failure", [smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"SECRET")}),
    smtplib.SMTPDataError(550, b"SECRET"), smtplib.SMTPAuthenticationError(535, b"SECRET"), RuntimeError("SECRET")])
def test_refusal_is_definite_and_generic(smtp, settings, failure):
    smtp[0].send_message.side_effect = failure
    with pytest.raises(mail.EmailError) as error:
        mail.send_report_pdf(PDF, "Report", "a@example.com", settings)
    assert not isinstance(error.value, mail.DeliveryUncertain) and "SECRET" not in str(error.value)
    smtp[0].send_message.assert_called_once()


def test_refusal_mapping_and_post_accept_disconnect(smtp, settings):
    smtp[0].send_message.return_value = {"a@example.com": (550, b"SECRET")}
    with pytest.raises(mail.EmailError):
        mail.send_report_pdf(PDF, "Report", "a@example.com", settings)
    smtp[0].send_message.return_value = {}
    smtp[0].quit.side_effect = smtplib.SMTPServerDisconnected("SECRET")
    assert mail.send_report_pdf(PDF, "Report", "a@example.com", settings) is None


@pytest.mark.parametrize("failure", [TimeoutError("SECRET"), OSError("SECRET"),
    ssl.SSLCertVerificationError("SECRET")])
def test_connection_failure_safe_and_no_send(smtp, settings, failure):
    smtp[1].side_effect = failure
    with pytest.raises(mail.EmailError) as error:
        mail.send_report_pdf(PDF, "Report", "a@example.com", settings)
    assert not isinstance(error.value, mail.DeliveryUncertain)
    assert "SECRET" not in str(error.value)
    smtp[0].send_message.assert_not_called()


def test_no_fallback_when_starttls_unsupported(smtp, settings):
    smtp[0].starttls.side_effect = smtplib.SMTPNotSupportedError("SECRET")
    with pytest.raises(mail.EmailError):
        mail.send_report_pdf(PDF, "Report", "a@example.com", settings)
    smtp[0].login.assert_not_called()
    smtp[0].send_message.assert_not_called()
    smtp[2].assert_not_called()


def test_invalid_recipient_and_title_rejected_before_connection(smtp, settings):
    for title, recipient in [("Report", "a@example.com,b@example.com"), (None, "a@example.com")]:
        with pytest.raises(mail.EmailError):
            mail.send_report_pdf(PDF, title, recipient, settings)
    smtp[1].assert_not_called()
    smtp[2].assert_not_called()


def test_store_default_path(monkeypatch, tmp_path):
    monkeypatch.setattr(mail, "ROOT", tmp_path)
    assert mail.RecipientStore().path == tmp_path / ".local" / "recipient_emails.json"


def test_store_persistence_hash_normalization_permissions_and_clear(tmp_path):
    path = tmp_path / "private" / "recipients.json"
    store = mail.RecipientStore(path)
    assert store.read("Profile") == "" and not path.exists()
    store.save("  Straẞe  ", "Person@EXAMPLE.COM")
    assert mail.RecipientStore(path).read("STRASSE") == "Person@example.com"
    key = hashlib.sha256("strasse".encode()).hexdigest()
    assert json.loads(path.read_text()) == {key: "Person@example.com"}
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]
    store.save("strasse", "   ")
    assert store.read("strasse") == "" and json.loads(path.read_text()) == {}


@pytest.mark.parametrize("value", [None, [], {"bad": "a@example.com"}, {"a" * 64: "bad"},
    {"a" * 64: None}, {"a" * 64: "A@EXAMPLE.COM"}, {"a" * 64: "a@example.com\r\n"}])
def test_store_rejects_corrupt_dictionary(tmp_path, value):
    path = tmp_path / "recipients.json"
    path.write_text(json.dumps(value))
    store = mail.RecipientStore(path)
    for operation in (lambda: store.read("name"), lambda: store.save("name", "a@example.com")):
        with pytest.raises(mail.EmailError):
            operation()
    assert json.loads(path.read_text()) == value


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b" " * (mail.MAX_STORE_BYTES + 1),
    ('{"' + 'a' * 64 + '":"a@example.com","' + 'a' * 64 + '":"b@example.com"}').encode()])
def test_store_rejects_invalid_or_oversized_json(tmp_path, raw):
    path = tmp_path / "recipients.json"
    path.write_bytes(raw)
    with pytest.raises(mail.EmailError):
        mail.RecipientStore(path).read("name")


def test_store_profile_limit_and_clear(tmp_path):
    path = tmp_path / "recipients.json"
    values = {hashlib.sha256(str(i).encode()).hexdigest(): "a@example.com" for i in range(100)}
    path.write_text(json.dumps(values))
    store = mail.RecipientStore(path)
    store.save("0", "updated@example.com")
    with pytest.raises(mail.EmailError):
        store.save("new", "a@example.com")
    store.save("0", "")
    store.save("new", "a@example.com")
    assert len(json.loads(path.read_text())) == 100
    values["f" * 64] = "a@example.com"
    path.write_text(json.dumps(values))
    with pytest.raises(mail.EmailError):
        store.read("0")


@pytest.mark.parametrize("target_kind", ["file", "directory", "ancestor", "dangling"])
def test_store_refuses_symlinks(tmp_path, target_kind):
    actual = tmp_path / "actual"
    actual.mkdir()
    target = actual / "recipients.json"
    target.write_text("{}")
    if target_kind in {"file", "dangling"}:
        path = tmp_path / "link.json"
        path.symlink_to(target if target_kind == "file" else actual / "missing")
    else:
        link = tmp_path / "linked"
        link.symlink_to(actual, target_is_directory=True)
        path = link / ("recipients.json" if target_kind == "directory" else "nested/recipients.json")
    store = mail.RecipientStore(path)
    with pytest.raises(mail.EmailError):
        store.read("name")
    with pytest.raises(mail.EmailError):
        store.save("name", "a@example.com")
    assert target.read_text() == "{}"


def test_store_refuses_nonregular_file(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(mail.EmailError):
        mail.RecipientStore(path).read("name")


def test_atomic_failure_preserves_file_and_removes_temp(monkeypatch, tmp_path):
    path = tmp_path / "private" / "recipients.json"
    store = mail.RecipientStore(path)
    store.save("name", "before@example.com")
    original = path.read_bytes()
    monkeypatch.setattr(mail.os, "replace", Mock(side_effect=OSError("SECRET path")))
    with pytest.raises(mail.EmailError) as error:
        store.save("name", "after@example.com")
    assert "SECRET" not in str(error.value)
    assert path.read_bytes() == original and list(path.parent.iterdir()) == [path]


def test_atomic_replace_and_existing_permissions(monkeypatch, tmp_path):
    path = tmp_path / "private" / "recipients.json"
    path.parent.mkdir(mode=0o755)
    path.write_text("{}")
    path.chmod(0o644)
    replace_file = Mock(wraps=mail.os.replace)
    monkeypatch.setattr(mail.os, "replace", replace_file)
    mail.RecipientStore(path).save("name", "a@example.com")
    replace_file.assert_called_once()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert list(path.parent.iterdir()) == [path]


def test_serialized_size_limit_preserves_existing_file(monkeypatch, tmp_path):
    path = tmp_path / "private" / "recipients.json"
    store = mail.RecipientStore(path)
    store.save("name", "a@example.com")
    original = path.read_bytes()
    monkeypatch.setattr(mail, "MAX_STORE_BYTES", len(original))
    with pytest.raises(mail.EmailError):
        store.save("another", "b@example.com")
    assert path.read_bytes() == original


def test_concurrent_instances_preserve_updates(tmp_path):
    path = tmp_path / "private" / "recipients.json"
    def save(index):
        mail.RecipientStore(path).save(str(index), f"user{index}@example.com")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save, range(40)))
    assert len(json.loads(path.read_text())) == 40
    assert len(path.read_bytes()) <= mail.MAX_STORE_BYTES


def test_invalid_preferences_do_not_write(tmp_path):
    path = tmp_path / "private" / "recipients.json"
    store = mail.RecipientStore(path)
    for profile, address in [("", "a@example.com"), (None, "a@example.com"), ("name", "bad"),
                             ("name", "\n"), ("name", None)]:
        with pytest.raises(mail.EmailError):
            store.save(profile, address)
    assert not path.exists()