"""Build-time check: sqlite-vec must load into the stdlib sqlite3 (skipped when
the rag extra was left out of the image)."""
import importlib.util
import sqlite3

if importlib.util.find_spec("sqlite_vec"):
    import sqlite_vec

    con = sqlite3.connect(":memory:")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    print("sqlite-vec", con.execute("select vec_version()").fetchone()[0])
