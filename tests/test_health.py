def test_health_returns_ok(make_client):
    def must_not_be_called(request):
        raise AssertionError("/health must not touch the upstream")

    client = make_client(must_not_be_called)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
