import sqlite3

try:
    # Use standard sqlite3 to force a checkpoint
    conn = sqlite3.connect('events.db')
    # This forces the WAL file to merge into the main .db file
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()
    print("Data merged successfully! You can now safely delete the -wal and -shm files.")
except Exception as e:
    print(f"Rescue failed: {e}")