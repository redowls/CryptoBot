"""Get full exception text from recent ERROR rows."""
from dotenv import load_dotenv
load_dotenv()
from db.connection import Database

db = Database()
df = db.query_df(
    "SELECT TOP 5 ts, module, message, exception FROM logs "
    "WHERE level = 'ERROR' ORDER BY ts DESC;"
)

for _, r in df.iterrows():
    print("=" * 70)
    print(f"{r['ts']}  {r['module']}")
    print(f"  message: {r['message']}")
    exc = r['exception'] or "(no exception column)"
    print(f"  exception:\n{exc}")
