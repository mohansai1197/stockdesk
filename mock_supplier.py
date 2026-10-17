"""A separate, fictional supplier API. Never a claimed real vendor integration."""

from fastapi import FastAPI

app = FastAPI(title="StockDesk Demo Supplier API")


@app.get("/catalog")
def catalog():
    return [
        {
            "Item Code": "SUP-NB-B5",
            "Product Name": "B5 Supplier Notebook",
            "Department": "Stationery",
            "Sell Price": "110.00",
            "Buy Price": "65.00",
            "Available": 30,
            "Reorder At": 8,
        },
        {
            "Item Code": "SUP-PEN-GR",
            "Product Name": "Green Gel Pen",
            "Department": "Stationery",
            "Sell Price": "25.00",
            "Buy Price": "12.00",
            "Available": 50,
            "Reorder At": 12,
        },
    ]


@app.get("/health")
def health():
    return {"status": "ok", "simulated": True}
