"""Saved lists API (/api/lists)."""
from app.models import Company, Contact, ListItem


def add_leads(db):
    a = Company(name="A", source="osm", source_ref="a")
    b = Company(name="B", source="osm", source_ref="b")
    db.add_all([a, b])
    db.flush()
    p = Contact(company_id=a.id, full_name="Ali")
    db.add(p)
    db.commit()
    return a, b, p


def test_create_list_and_duplicate_name(client):
    r = client.post("/api/lists", json={"name": "  Miami   dentists ", "description": "call this week"})
    assert r.status_code == 201
    created = r.json()
    assert created["name"] == "Miami dentists" and created["description"] == "call this week"
    assert created["company_count"] == 0 and created["contact_count"] == 0 and created["created_at"].endswith(("Z", "+00:00"))
    r = client.post("/api/lists", json={"name": "miami DENTISTS"})
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    assert client.post("/api/lists", json={"name": "   "}).status_code == 422
    assert client.post("/api/lists", json={"name": ""}).status_code == 422


def test_list_lists_sorted_with_counts(client, db):
    a, b, p = add_leads(db)
    zed = client.post("/api/lists", json={"name": "zed"}).json()
    client.post("/api/lists", json={"name": "Alpha"})
    r = client.post(f"/api/lists/{zed['id']}/items", json={"company_ids": [a.id, b.id], "contact_ids": [p.id]})
    assert r.json() == {"changed": 3}
    lists = client.get("/api/lists").json()
    assert [x["name"] for x in lists] == ["Alpha", "zed"]
    assert (lists[1]["company_count"], lists[1]["contact_count"]) == (2, 1)
    assert (lists[0]["company_count"], lists[0]["contact_count"]) == (0, 0)


def test_add_items_dedupes_and_ignores_unknown(client, db):
    a, b, p = add_leads(db)
    lid = client.post("/api/lists", json={"name": "L"}).json()["id"]
    r = client.post(f"/api/lists/{lid}/items", json={"company_ids": [a.id, a.id, 999], "contact_ids": [p.id, 888]})
    assert r.json() == {"changed": 2}
    r = client.post(f"/api/lists/{lid}/items", json={"company_ids": [a.id, b.id], "contact_ids": [p.id]})
    assert r.json() == {"changed": 1}  # only B is new
    assert db.query(ListItem).count() == 3
    # Defaults: both id lists optional
    assert client.post(f"/api/lists/{lid}/items", json={}).json() == {"changed": 0}


def test_remove_items(client, db):
    a, b, p = add_leads(db)
    lid = client.post("/api/lists", json={"name": "L"}).json()["id"]
    client.post(f"/api/lists/{lid}/items", json={"company_ids": [a.id, b.id], "contact_ids": [p.id]})
    r = client.post(f"/api/lists/{lid}/items/remove", json={"company_ids": [a.id, 999], "contact_ids": [p.id]})
    assert r.json() == {"changed": 2}
    assert client.get("/api/lists").json()[0]["company_count"] == 1


def test_missing_list_is_404(client):
    assert client.post("/api/lists/42/items", json={"company_ids": [1]}).status_code == 404
    assert client.post("/api/lists/42/items/remove", json={}).status_code == 404
    assert client.patch("/api/lists/42", json={"name": "x"}).status_code == 404
    assert client.delete("/api/lists/42").status_code == 404


def test_update_list(client):
    first = client.post("/api/lists", json={"name": "One", "description": "d"}).json()
    client.post("/api/lists", json={"name": "Two"})
    r = client.patch(f"/api/lists/{first['id']}", json={"name": "Uno"})
    assert r.status_code == 200 and r.json()["name"] == "Uno" and r.json()["description"] == "d"
    assert client.patch(f"/api/lists/{first['id']}", json={"name": "two"}).status_code == 409
    # Renaming to its own name (different case) is fine
    assert client.patch(f"/api/lists/{first['id']}", json={"name": "UNO"}).status_code == 200
    r = client.patch(f"/api/lists/{first['id']}", json={"description": None})
    assert r.json()["description"] is None and r.json()["name"] == "UNO"


def test_delete_list_keeps_leads(client, db):
    a, b, p = add_leads(db)
    lid = client.post("/api/lists", json={"name": "L"}).json()["id"]
    client.post(f"/api/lists/{lid}/items", json={"company_ids": [a.id], "contact_ids": [p.id]})
    r = client.delete(f"/api/lists/{lid}")
    assert r.status_code == 204 and r.content == b""
    assert client.get("/api/lists").json() == []
    db.expire_all()
    assert db.query(ListItem).count() == 0
    assert db.query(Company).count() == 2 and db.query(Contact).count() == 1
