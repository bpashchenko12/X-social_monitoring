import unittest

from pipeline import alert

ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "u", "SMTP_PASSWORD": "hunter2hunter2",
       "ALERT_EMAIL_TO": "a@example.com, b@example.com", "ALERT_EMAIL_FROM": "bot@example.com",
       "OPENROUTER_API_KEY": "sk-or-abcdef123456"}


class FakeSMTP:
    instances = []
    fail = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.sent, self.tls, self.login_args = host, port, [], False, None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, u, p):
        self.login_args = (u, p)

    def send_message(self, msg):
        self.sent.append(msg)


class FakeSSL(FakeSMTP):
    pass


class AlertTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances, FakeSMTP.fail = [], None

    def send(self, subject="s", body="b", env=ENV):
        return alert.send_email(subject, body, env, smtp_cls=FakeSMTP, smtp_ssl_cls=FakeSSL)

    def test_not_configured_does_not_raise(self):
        sent, err = self.send(env={})
        self.assertFalse(sent)
        self.assertIn("not configured", err)
        self.assertEqual(FakeSMTP.instances, [])

    def test_sends_with_starttls_and_login(self):
        sent, err = self.send("subject", "body")
        self.assertEqual((sent, err), (True, ""))
        s = FakeSMTP.instances[0]
        self.assertTrue(s.tls)
        self.assertEqual(s.login_args, ("u", "hunter2hunter2"))
        self.assertEqual(s.sent[0]["To"], "a@example.com, b@example.com")

    def test_port_465_uses_ssl_without_starttls(self):
        self.send(env=dict(ENV, SMTP_PORT="465"))
        self.assertEqual(len(FakeSSL.instances), 1)
        self.assertIsInstance(FakeSMTP.instances[0], FakeSSL)
        self.assertFalse(FakeSMTP.instances[0].tls)

    def test_smtp_failure_is_returned_not_raised_and_scrubbed(self):
        FakeSMTP.fail = RuntimeError("auth failed for hunter2hunter2")
        sent, err = self.send()
        self.assertFalse(sent)
        self.assertNotIn("hunter2hunter2", err)

    def test_header_injection_is_neutralised(self):
        self.send(subject="hi\r\nBcc: evil@example.com")
        msg = FakeSMTP.instances[0].sent[0]
        self.assertIsNone(msg["Bcc"])
        self.assertNotIn("\n", msg["Subject"])

    def test_secrets_scrubbed_from_body_and_subject(self):
        self.send(subject="key sk-or-abcdef123456", body="pw hunter2hunter2 key sk-or-abcdef123456")
        msg = FakeSMTP.instances[0].sent[0]
        text = msg["Subject"] + msg.get_content()
        self.assertNotIn("hunter2hunter2", text)
        self.assertNotIn("sk-or-abcdef123456", text)

    def test_huge_body_is_capped(self):
        self.send(body="x" * 100000)
        self.assertLess(len(FakeSMTP.instances[0].sent[0].get_content()), alert.MAX_BODY + 10)

    def test_bad_port_does_not_raise(self):
        sent, err = self.send(env=dict(ENV, SMTP_PORT="abc"))
        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()
