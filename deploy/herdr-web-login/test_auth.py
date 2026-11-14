import http.client
import http.cookies
import importlib.util
import os
import threading
import unittest

os.environ["HERDR_LOGIN_PASSWORD_SHA256"] = __import__("hashlib").sha256(b"correct-password").hexdigest()
spec = importlib.util.spec_from_file_location("auth", os.path.join(os.path.dirname(__file__), "auth.py"))
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class LoginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = auth.http.server.ThreadingHTTPServer(("127.0.0.1", 0), auth.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        with auth.ATTEMPTS_LOCK:
            auth.ATTEMPTS.clear()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_form_uses_password_manager_fields(self):
        status, headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertIn(b'autocomplete="username"', body)
        self.assertIn(b'type="password"', body)
        self.assertIn(b'autocomplete="current-password"', body)
        self.assertIn(b'method="post"', body)

    def test_wrong_password_returns_form_without_cookie(self):
        status, headers, body = self.request("POST", "/login", "username=fanyun&password=wrong", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIn(b"incorrect", body)

    def test_success_sets_secure_cookie_and_redirects(self):
        status, headers, _ = self.request("POST", "/login?next=/", "username=fanyun&password=correct-password", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)
        session = http.cookies.SimpleCookie(cookie)
        value = session[auth.COOKIE_NAME].value
        status, _, _ = self.request("GET", "/verify", headers={"Cookie": f"{auth.COOKIE_NAME}={value}"})
        self.assertEqual(status, 200)

    def test_no_cookie_or_tampered_cookie_fails_verification(self):
        status, headers, _ = self.request("GET", "/verify")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login?next=%2F")
        status, headers, _ = self.request("GET", "/verify", headers={"Cookie": f"{auth.COOKIE_NAME}=bad", "X-Forwarded-Uri": "/app.css"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login?next=%2Fapp.css")

    def test_external_redirect_is_rejected(self):
        status, headers, _ = self.request("POST", "/login?next=https://evil.example", "username=fanyun&password=correct-password", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")

    def test_logout_clears_cookie(self):
        status, headers, _ = self.request("GET", "/logout")
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_guessing_is_rate_limited(self):
        for _ in range(auth.MAX_FAILURES):
            self.request("POST", "/login", "username=fanyun&password=wrong", {"Content-Type": "application/x-www-form-urlencoded"})
        status, headers, _ = self.request("POST", "/login", "username=fanyun&password=correct-password", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)

    def test_domain_host_keeps_secure_cookie_even_over_plain_http(self):
        status, headers, _ = self.request(
            "POST", "/login?next=/",
            "username=fanyun&password=correct-password",
            {"Content-Type": "application/x-www-form-urlencoded",
             "Host": "herdr-mldev.byted.org", "X-Forwarded-Proto": "http"})
        self.assertEqual(status, 303)
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_remote_ip_http_origin_drops_only_secure_but_keeps_hardening(self):
        status, headers, _ = self.request(
            "POST", "/login?next=/",
            "username=fanyun&password=correct-password",
            {"Content-Type": "application/x-www-form-urlencoded",
             "Host": "[2001:db8::1]:80", "X-Forwarded-Proto": "http"})
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        # The signature is unchanged, so the cookie still authenticates.
        session = http.cookies.SimpleCookie(cookie)
        value = session[auth.COOKIE_NAME].value
        status, _, _ = self.request("GET", "/verify", headers={"Cookie": f"{auth.COOKIE_NAME}={value}"})
        self.assertEqual(status, 200)

    def test_remote_ip_https_origin_keeps_secure(self):
        status, headers, _ = self.request(
            "POST", "/login?next=/",
            "username=fanyun&password=correct-password",
            {"Content-Type": "application/x-www-form-urlencoded",
             "Host": "[2001:db8::1]:443", "X-Forwarded-Proto": "https"})
        self.assertEqual(status, 303)
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_loopback_http_origin_keeps_secure(self):
        status, headers, _ = self.request(
            "POST", "/login?next=/",
            "username=fanyun&password=correct-password",
            {"Content-Type": "application/x-www-form-urlencoded",
             "Host": "127.0.0.1:8377", "X-Forwarded-Proto": "http"})
        self.assertEqual(status, 303)
        self.assertIn("Secure", headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
