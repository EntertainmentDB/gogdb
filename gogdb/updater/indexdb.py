import json
import dataclasses
import os
import collections
import logging

import aiosqlite

from gogdb.core.normalization import normalize_search, compress_systems
import gogdb.core.storage as storage
import gogdb.core.model as model



logger = logging.getLogger("UpdateDB.Index")

async def init_db(cur):
    await cur.execute("""CREATE TABLE products (
        product_id INTEGER,
        title TEXT,
        image_logo TEXT,
        product_type TEXT,
        comp_systems TEXT,
        sale_rank INTEGER,
        search_title TEXT
    );""")
    await cur.execute("""CREATE TABLE changelog (
        product_id INTEGER,
        product_title TEXT,
        timestamp REAL,
        action TEXT,
        category TEXT,
        dl_type TEXT,
        bonus_type TEXT,
        property_name TEXT,
        serialized_record TEXT
    );""")
    await cur.execute("""CREATE TABLE changelog_summary (
        product_id INTEGER,
        product_title TEXT,
        timestamp REAL,
        categories TEXT
    );""")
    await cur.execute("CREATE INDEX idx_products_sale_rank ON products (sale_rank)")
    await cur.execute("CREATE INDEX idx_changelog_timestamp ON changelog (timestamp)")
    await cur.execute("CREATE INDEX idx_summary_timestamp ON changelog_summary (timestamp)")

async def count_rows(cur, table_name):
    await cur.execute(f"SELECT COUNT(*) FROM {table_name};")
    return (await cur.fetchone())[0]

async def index_product(prod, cur):
    if prod.rank_bestselling is not None:
        sale_rank = prod.rank_bestselling
    else:
        sale_rank = 100000  # some high number
    await cur.execute(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            prod.id,
            prod.title,
            prod.image_logo,
            prod.type,
            compress_systems(prod.comp_systems),
            sale_rank,
            normalize_search(prod.title)
        )
    )

async def index_changelog(prod, changelog, cur):
    summaries = collections.defaultdict(set)
    for changerec in changelog:
        idx_change = model.IndexChange(
            id = prod.id,
            title = prod.title,
            timestamp = changerec.timestamp,
            action = changerec.action,
            category = changerec.category,
            record = changerec
        )
        if changerec.category == "download":
            idx_change.dl_type = changerec.download_record.dl_type
            if changerec.download_record.dl_new_bonus is not None:
                idx_change.bonus_type = changerec.download_record.dl_new_bonus.bonus_type
            if changerec.download_record.dl_old_bonus is not None:
                # Just set it potentially twice because it has to be the same value anyway
                idx_change.bonus_type = changerec.download_record.dl_old_bonus.bonus_type
        elif changerec.category == "property":
            idx_change.property_name = changerec.property_record.property_name

        await cur.execute(
            "INSERT INTO changelog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                idx_change.id,
                idx_change.title,
                idx_change.timestamp.timestamp(),
                idx_change.action,
                idx_change.category,
                idx_change.dl_type,
                idx_change.bonus_type,
                idx_change.property_name,
                json.dumps(
                    idx_change.record, sort_keys=True, ensure_ascii=False,
                    default=storage.json_encoder)
            )
        )

        summaries[changerec.timestamp].add(changerec.category)

    for timestamp, category_set in summaries.items():
        category_str = ",".join(sorted(category_set))
        await cur.execute(
            "INSERT INTO changelog_summary VALUES (?, ?, ?, ?)",
            (
                prod.id,
                prod.title,
                timestamp.timestamp(),
                category_str
            )
        )

class IndexDbProcessor:
    wants = {"product", "changelog"}

    def __init__(self, db):
        self.db = db
        self.indexdb_path = db.path_indexdb()
        self.indexdb_path_part = self.indexdb_path.with_name(self.indexdb_path.name + ".part")
        self.conn = None
        self.cur = None

    async def prepare(self):
        self.indexdb_path.parent.mkdir(exist_ok=True)
        self.indexdb_path_part.unlink(missing_ok=True)
        self.conn = await aiosqlite.connect(self.indexdb_path_part, isolation_level=None)
        self.cur = await self.conn.cursor()
        await init_db(self.cur)

        await self.cur.execute("BEGIN TRANSACTION;")
        await self.cur.execute("DELETE FROM products;")
        await self.cur.execute("DELETE FROM changelog;")
        await self.cur.execute("DELETE FROM changelog_summary;")

    async def process(self, data):
        if data.product is None:
            return
        await index_product(data.product, self.cur)

        if data.changelog is None:
            return
        await index_changelog(data.product, data.changelog, self.cur)

    async def finish(self):
        await self.cur.execute("END TRANSACTION;")

        print("Indexed {} products, {} changelog entries, {} changelog summaries".format(
            await count_rows(self.cur, "products"),
            await count_rows(self.cur, "changelog"),
            await count_rows(self.cur, "changelog_summary")
        ))

        await self.cur.close()
        await self.conn.commit()
        await self.conn.close()
        os.replace(src=self.indexdb_path_part, dst=self.indexdb_path)
