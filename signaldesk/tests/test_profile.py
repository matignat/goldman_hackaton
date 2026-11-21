def test_profile_update_is_scoped_to_authenticated_user(client, login):
    path = "/api/me/profile"
    changes = {"name": "Updated User", "email": "updated@example.test", "phone": ""}
    assert client.put(path, json=changes).status_code == 401
    login(2)
    original = client.get("/api/me").json()
    for invalid in ({"role": "A"}, {"user_id": 1}, {"email": "invalid"}, {"name": "  "}):
        assert client.put(path, json={**changes, **invalid}).status_code == 422
    assert client.put(path, json={**changes, "name": "Morgan Blake"}).status_code == 409
    assert client.get("/api/me").json() == original
    assert client.put(path, json=changes).is_success
    profile = client.get("/api/me").json()
    assert all(profile[key] == value for key, value in changes.items())
    assert profile["id"] == original["id"] and profile["role"] == original["role"]
    assert login(1).get("/api/me").json()["name"] == "Morgan Blake"
