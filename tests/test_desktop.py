from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import desktop

class DesktopTests(unittest.TestCase):
    def test_copy_existing_database_without_overwriting(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root=Path(directory)
            old=root/'old'/'data'/'rides.sqlite3'
            old.parent.mkdir(parents=True)
            with closing(sqlite3.connect(old)) as source:
                source.execute('CREATE TABLE example (value TEXT)')
                source.execute("INSERT INTO example VALUES ('existing')")
                source.commit()
            target=root/'profile'/'data'/'rides.sqlite3'
            with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(root/'old'/'RailwayJournal.exe')),patch.object(app,'DB',target):
                desktop.migrate_local_data()
                with closing(sqlite3.connect(target)) as database:
                    self.assertEqual(database.execute('SELECT value FROM example').fetchone()[0],'existing')
                    database.execute("UPDATE example SET value='new'")
                    database.commit()
                desktop.migrate_local_data()
                with closing(sqlite3.connect(target)) as database:
                    self.assertEqual(database.execute('SELECT value FROM example').fetchone()[0],'new')
            with closing(sqlite3.connect(old)) as database:
                self.assertEqual(database.execute('SELECT value FROM example').fetchone()[0],'existing')
