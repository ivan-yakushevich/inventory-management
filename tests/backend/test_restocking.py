"""
Tests for restocking order API endpoints.
"""
from datetime import datetime, timedelta

import pytest

import main


@pytest.fixture(autouse=True)
def reset_restock_orders():
    """Clear the in-memory restock orders so each test starts from an empty list."""
    main.restock_orders.clear()
    yield
    main.restock_orders.clear()


@pytest.fixture
def forecasts(client):
    """Demand forecasts keyed by SKU."""
    response = client.get("/api/demand")
    return {f["item_sku"]: f for f in response.json()}


class TestRestockOrderEndpoints:
    """Test suite for restocking order endpoints."""

    def test_get_restock_orders_empty(self, client):
        """Test that no restock orders exist before any are submitted."""
        response = client.get("/api/restock-orders")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_create_restock_order(self, client, forecasts):
        """Test submitting a valid restock order."""
        payload = {
            "budget": 100000,
            "items": [
                {"sku": "WDG-001", "quantity": 100},
                {"sku": "GSK-203", "quantity": 50}
            ]
        }
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 201

        order = response.json()
        assert order["order_number"].startswith("RST-")
        assert order["status"] == "Submitted"
        assert order["budget"] == 100000
        assert len(order["items"]) == 2

        for item in order["items"]:
            assert "sku" in item
            assert "name" in item
            assert "quantity" in item
            assert "unit_cost" in item
            assert "line_total" in item
            assert "lead_time_days" in item

    def test_restock_order_total_cost_calculation(self, client, forecasts):
        """Test that the total cost is computed from server-side unit costs."""
        payload = {
            "budget": 100000,
            "items": [
                {"sku": "WDG-001", "quantity": 100},
                {"sku": "GSK-203", "quantity": 50}
            ]
        }
        order = client.post("/api/restock-orders", json=payload).json()

        expected_total = (
            100 * forecasts["WDG-001"]["unit_cost"]
            + 50 * forecasts["GSK-203"]["unit_cost"]
        )
        assert abs(order["total_cost"] - expected_total) < 0.01

    def test_restock_order_lead_time_calculation(self, client, forecasts):
        """Test that lead time is the slowest item's lead time and drives expected delivery."""
        payload = {
            "budget": 100000,
            "items": [
                {"sku": "WDG-001", "quantity": 10},
                {"sku": "MTR-304", "quantity": 1},
                {"sku": "FLT-405", "quantity": 10}
            ]
        }
        order = client.post("/api/restock-orders", json=payload).json()

        expected_lead_time = max(
            forecasts[sku]["lead_time_days"] for sku in ["WDG-001", "MTR-304", "FLT-405"]
        )
        assert order["lead_time_days"] == expected_lead_time

        order_date = datetime.fromisoformat(order["order_date"])
        expected_delivery = datetime.fromisoformat(order["expected_delivery"])
        assert expected_delivery - order_date == timedelta(days=expected_lead_time)

    def test_restock_order_ignores_client_prices(self, client, forecasts):
        """Test that prices sent by the client are ignored."""
        payload = {
            "budget": 100000,
            "items": [{"sku": "WDG-001", "quantity": 10, "unit_cost": 0.01}]
        }
        order = client.post("/api/restock-orders", json=payload).json()

        assert order["items"][0]["unit_cost"] == forecasts["WDG-001"]["unit_cost"]
        assert abs(order["total_cost"] - 10 * forecasts["WDG-001"]["unit_cost"]) < 0.01

    def test_created_restock_order_appears_in_list(self, client):
        """Test that submitted orders are listed, newest first."""
        first = client.post(
            "/api/restock-orders",
            json={"budget": 100000, "items": [{"sku": "WDG-001", "quantity": 1}]}
        ).json()
        second = client.post(
            "/api/restock-orders",
            json={"budget": 100000, "items": [{"sku": "GSK-203", "quantity": 1}]}
        ).json()

        response = client.get("/api/restock-orders")
        data = response.json()

        assert [o["id"] for o in data] == [second["id"], first["id"]]
        assert first["order_number"] != second["order_number"]

    def test_create_restock_order_unknown_sku(self, client):
        """Test that an unknown SKU is rejected."""
        payload = {"budget": 100000, "items": [{"sku": "NOPE-999", "quantity": 5}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 400

        data = response.json()
        assert "detail" in data
        assert "unknown sku" in data["detail"].lower()

    def test_create_restock_order_zero_quantity(self, client):
        """Test that a zero quantity fails validation."""
        payload = {"budget": 100000, "items": [{"sku": "WDG-001", "quantity": 0}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 422

    def test_create_restock_order_no_items(self, client):
        """Test that an order without items is rejected."""
        response = client.post("/api/restock-orders", json={"budget": 1000, "items": []})
        assert response.status_code == 400

    def test_create_restock_order_duplicate_sku(self, client):
        """Test that the same SKU twice in one order is rejected."""
        payload = {
            "budget": 100000,
            "items": [
                {"sku": "WDG-001", "quantity": 5},
                {"sku": "WDG-001", "quantity": 3}
            ]
        }
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 400
        assert "once" in response.json()["detail"].lower()

    def test_create_restock_order_invalid_payloads(self, client):
        """Test that negative or missing fields fail validation."""
        bad_payloads = [
            {"budget": 1000, "items": [{"sku": "WDG-001", "quantity": -1}]},
            {"budget": -1, "items": [{"sku": "WDG-001", "quantity": 1}]},
            {"items": [{"sku": "WDG-001", "quantity": 1}]},
            {"budget": 1000}
        ]
        for payload in bad_payloads:
            response = client.post("/api/restock-orders", json=payload)
            assert response.status_code == 422, payload

    def test_create_restock_order_total_equals_budget(self, client, forecasts):
        """Test that an order costing exactly the budget is accepted despite float-prone prices."""
        # 937 x $12.80 is the partial line the UI produces at a $12,000 budget
        quantity = 937
        budget = round(quantity * forecasts["FLT-405"]["unit_cost"], 2)
        payload = {"budget": budget, "items": [{"sku": "FLT-405", "quantity": quantity}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 201
        assert abs(response.json()["total_cost"] - budget) < 0.01

    def test_create_restock_order_one_cent_over_budget(self, client, forecasts):
        """Test that an order one cent over the budget is rejected."""
        cost = forecasts["FLT-405"]["unit_cost"]
        payload = {"budget": round(10 * cost - 0.01, 2), "items": [{"sku": "FLT-405", "quantity": 10}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 400

    def test_restock_order_number_sequence(self, client):
        """Test that order numbers follow RST-<year>-NNNN and increment."""
        payload = {"budget": 100000, "items": [{"sku": "GSK-203", "quantity": 1}]}
        first = client.post("/api/restock-orders", json=payload).json()
        second = client.post("/api/restock-orders", json=payload).json()

        year = datetime.fromisoformat(first["order_date"]).year
        assert first["order_number"] == f"RST-{year}-0001"
        assert second["order_number"] == f"RST-{year}-0002"

    def test_create_restock_order_over_budget(self, client):
        """Test that an order costing more than the budget is rejected and not stored."""
        payload = {"budget": 10, "items": [{"sku": "MTR-304", "quantity": 5}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 400

        data = response.json()
        assert "budget" in data["detail"].lower()

        assert client.get("/api/restock-orders").json() == []
