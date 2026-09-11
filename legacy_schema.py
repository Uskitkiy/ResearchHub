import sqlite3
def initialize_legacy(path):
    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            school TEXT DEFAULT 'Школа не указана',
            bio TEXT DEFAULT 'Исследователь и олимпиадник',
            achievements TEXT DEFAULT 'Пока нет наград',
            badge TEXT DEFAULT '🌱 Исследователь'
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            image TEXT,
            filename TEXT,
            filepath TEXT,
            author TEXT NOT NULL,
            likes INTEGER DEFAULT 0,
            post_type TEXT DEFAULT 'research',
            book_author TEXT DEFAULT '',
            volume TEXT DEFAULT '',
            roles_needed TEXT DEFAULT '',
            subject TEXT DEFAULT '⚛️ Физика',
            grade_level TEXT DEFAULT 'Все уровни',
            difficulty TEXT DEFAULT '🟡 Область / Респа',
            olympiad_name TEXT DEFAULT '',
            hint TEXT DEFAULT '',
            status TEXT DEFAULT '❓ В поиске решения',
            subcategory TEXT DEFAULT '',
            book_reference TEXT DEFAULT '',
            abstract TEXT DEFAULT '',
            methodology TEXT DEFAULT '',
            references_list TEXT DEFAULT '',
            coauthors TEXT DEFAULT '',
            solution_filename TEXT DEFAULT '',
            solution_filepath TEXT DEFAULT ''
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            parent_id INTEGER DEFAULT NULL,
            author TEXT NOT NULL,
            text TEXT NOT NULL,
            image TEXT,
            filename TEXT,
            filepath TEXT,
            is_accepted INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bookmarks (
            username TEXT NOT NULL,
            post_id INTEGER NOT NULL,
            PRIMARY KEY (username, post_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            reporter TEXT NOT NULL,
            reason TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            applicant TEXT NOT NULL,
            role_applied TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
        )
    ''')

    # Авто-миграции колонок ЕМН
    migrations = [
        "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN badge TEXT DEFAULT '🌱 Исследователь'",
        "ALTER TABLE posts ADD COLUMN subject TEXT DEFAULT '⚛️ Физика'",
        "ALTER TABLE posts ADD COLUMN grade_level TEXT DEFAULT 'Все уровни'",
        "ALTER TABLE posts ADD COLUMN difficulty TEXT DEFAULT '🟡 Область / Респа'",
        "ALTER TABLE posts ADD COLUMN olympiad_name TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN hint TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN status TEXT DEFAULT '❓ В поиске решения'",
        "ALTER TABLE posts ADD COLUMN subcategory TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN book_reference TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN abstract TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN methodology TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN references_list TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN coauthors TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN solution_filename TEXT DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN solution_filepath TEXT DEFAULT ''"
    ]
    for migration in migrations:
        try:
            cursor.execute(migration)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()
