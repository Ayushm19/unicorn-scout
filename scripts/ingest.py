"""One-off: clean the CSV, embed every company, and upsert into Neon. Safe to re-run.

Usage: python -m scripts.ingest [path/to/tracxn.csv]
"""
import sys
from pathlib import Path

from app import db
from app.data import embedding_text, load_companies
from app.embeddings import embed_documents


def main(csv_path: str) -> None:
    companies = load_companies(csv_path)
    print(f"Loaded {len(companies)} companies from {csv_path}")
    db.init_schema()
    vectors = embed_documents([embedding_text(c) for c in companies])
    db.upsert_companies(companies, vectors)
    print(f"Upserted {len(companies)} companies with {len(vectors[0])}-dim embeddings")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent.parent / "data" / "tracxn.csv"))
