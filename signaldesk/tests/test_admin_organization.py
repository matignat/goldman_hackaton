def test_create_team_with_first_manager(login):
    payload = {"name": "Market Operations", "leader_id": 2}
    assert login(1).post("/api/admin/teams", json=payload).status_code == 403
    admin = login(5)
    assert admin.post("/api/admin/teams", json={**payload, "leader_id": 5}).status_code == 422
    response = admin.post("/api/admin/teams", json=payload)
    assert response.status_code == 201
    team = response.json()
    assert team["leader_id"] == 2 and [member["id"] for member in team["members"]] == [2]
    assert admin.post("/api/admin/teams", json=payload).status_code == 409
    manager = login(2)
    assert team["id"] in [
        t["id"] for t in manager.get("/api/organization").json()["managed_teams"]
    ]
    assert manager.get("/api/policies", params={"team": team["id"]}).json()


def test_explicit_reporting_lines_are_independent_of_team_membership(login):
    def reports(uid):
        return [p["id"] for p in login(uid).get("/api/organization").json()["reports"]]

    path = "/api/admin/users/2/manager"
    assert login(1).put(path, json={"manager_id": 4}).status_code == 403
    assert login(5).put(path, json={"manager_id": 4}).is_success
    assert login(2).get("/api/organization").json()["manager"]["id"] == 4
    assert 2 in reports(4) and 2 not in reports(1)
    assert login(5).put(path, json={"manager_id": None}).is_success
    assert login(2).get("/api/organization").json()["manager"] is None
    assert 2 not in reports(1) and 2 not in reports(4)


def test_direct_manager_rejects_invalid_and_circular_assignments(login):
    admin = login(5)
    path = "/api/admin/users/1/manager"
    assert admin.put(path, json={"manager_id": 2}).status_code == 422
    assert admin.put(path, json={"manager_id": 1}).status_code == 409
    assert admin.put(path, json={"manager_id": 4}).is_success
    assert admin.put("/api/admin/users/4/manager", json={"manager_id": 1}).status_code == 409
