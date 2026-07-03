import ssl

from plugin.framework.client.request_controls import LocalHttpsCertificateFallback, RequestPacer


def test_request_pacer_sleeps_for_back_to_back_sends():
    sleeps: list[float] = []
    times = iter([1000.0, 1000.0, 1000.0])
    pacer = RequestPacer(monotonic=lambda: next(times), sleep=sleeps.append)

    pacer.wait_before_send()
    pacer.mark_sent()
    pacer.wait_before_send()

    assert sleeps == [0.05]


def test_local_https_certificate_fallback_only_enables_for_local_cert_errors():
    fallback = LocalHttpsCertificateFallback()

    assert fallback.ssl_mode_for("https", "localhost") == "verified"
    assert fallback.enable_if_applicable("localhost", ssl.SSLCertVerificationError("self-signed certificate")) is True
    assert fallback.ssl_mode_for("https", "localhost") == "unverified"

    assert fallback.enable_if_applicable("api.openai.com", ssl.SSLCertVerificationError("self-signed certificate")) is False
    assert fallback.enable_if_applicable("localhost", OSError("connection reset")) is False
    assert fallback.ssl_mode_for("http", "localhost") == "plain"
