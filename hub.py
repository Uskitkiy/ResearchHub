"""Local Flask/SQLite application. Existing tables and attachments are migrated in place."""

import os, json, sqlite3, secrets, re, uuid, hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, request, session, jsonify, g, render_template, send_from_directory, abort
from werkzeug.security import generate_password_hash as werkzeug_generate_password_hash, check_password_hash as werkzeug_check_password_hash
from werkzeug.exceptions import HTTPException
from legacy_schema import initialize_legacy

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('HUB_DATABASE', str(ROOT / 'database.db')))
UPLOADS = Path(os.environ.get('HUB_UPLOADS', str(ROOT / 'uploads')))
UPLOADS.mkdir(parents=True, exist_ok=True)
secret_file = ROOT / '.session-secret'
if not os.environ.get('SECRET_KEY') and not secret_file.exists():
    secret_file.write_text(secrets.token_hex(32), encoding='utf-8')
app = Flask(__name__, template_folder=str(ROOT), static_folder=str(ROOT / 'static'))
app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY') or secret_file.read_text().strip(),
    MAX_CONTENT_LENGTH=12*1024*1024, SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(days=7))
CONFIG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
TYPES = {'work':'Работа','photo':'Фото','task':'Задача','idea':'Идея'}

def generate_password_hash(password):
    # Explicit portable method: some macOS Python builds have no hashlib.scrypt.
    return werkzeug_generate_password_hash(password,method='pbkdf2:sha256:1000000')

def check_password_hash(stored,password):
    if stored.startswith('scrypt:') and not callable(getattr(hashlib,'scrypt',None)):
        fail('Этот аккаунт использует scrypt, недоступный в текущей сборке Python. Запустите сайт в Python с поддержкой hashlib.scrypt. Пароль и данные аккаунта сохранены.',503)
    return werkzeug_check_password_hash(stored,password)

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def active_mute(user):
    return bool(user and user.get('muted_until') and datetime.fromisoformat(user['muted_until'])>datetime.now(timezone.utc))

def account_age(created_at):
    if not created_at:return None
    registered=datetime.fromisoformat(created_at)
    if registered.tzinfo is None:registered=registered.replace(tzinfo=timezone.utc)
    return max(0,(datetime.now(timezone.utc)-registered).days)
def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB, timeout=20)
        g.db.row_factory = sqlite3.Row
        g.db.create_function('casefold',1,lambda s:(s or '').casefold())
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db
@app.teardown_appcontext
def close_db(error):
    c=g.pop('db',None)
    if c: c.close()
def rows(sql,args=()): return [dict(r) for r in db().execute(sql,args).fetchall()]
def one(sql,args=()):
    r=db().execute(sql,args).fetchone()
    return dict(r) if r else None
def fail(message,code=400): abort(code,description=message)
def data():
    if not request.is_json:return request.form
    value=request.get_json(silent=True)
    if not isinstance(value,dict):fail('Ожидается JSON-объект с полями формы')
    return value
def text(d,key,maximum=10000,required=False):
    v=d.get(key,'')
    if not isinstance(v,str): fail('Неверный формат поля '+key)
    v=v.strip()
    label={'title':'Заголовок','username':'Имя пользователя','password':'Пароль','content':'Содержание','text':'Комментарий','name':'Название','description':'Описание','reason':'Причина','resolution':'Решение'}.get(key,key)
    if len(v)>maximum: fail(f'{label}: максимум {maximum} символов')
    if required and not v: fail('Заполните поле «'+label+'»')
    return v
def integer(v):
    if isinstance(v,bool) or not re.fullmatch(r'-?\d+',str(v)):fail('Ожидается целое число')
    try: return int(v)
    except (ValueError,TypeError): fail('Ожидается целое число')
def member():
    if not g.user: fail('Войдите в аккаунт',401)
    return g.user['username']
def staff(admin=False):
    member()
    if g.user['role'] not in (['admin'] if admin else ['admin','moderator']): fail('Недостаточно прав',403)
def owner(item):
    if item['author'] != member(): fail('Можно изменять только собственные материалы',403)
def manage_post(item):
    username=member()
    if item['author'] != username and g.user['role'] != 'admin':
        fail('Можно изменять только собственные материалы',403)
def post(pid):
    p=one('SELECT * FROM posts WHERE id=? AND deleted_at IS NULL',(pid,))
    if not p or (p['publication_status']=='draft' and p['author'] != session.get('username')): fail('Публикация не найдена',404)
    return p
def audit(action,entity,details=None):
    db().execute('INSERT INTO audit(actor,action,entity,created_at,details) VALUES(?,?,?,?,?)',
        (member(),action,str(entity),now(),json.dumps(details or {},ensure_ascii=False)))

def migrate():
    initialize_legacy(DB)
    with sqlite3.connect(DB) as c:
        c.execute('PRAGMA journal_mode=WAL')
        additions={
            'users': {'created_at':'TEXT','display_name':"TEXT DEFAULT ''",'avatar':"TEXT DEFAULT ''",'muted_until':'TEXT','mute_reason':"TEXT DEFAULT ''",'deleted_at':'TEXT'},
            'posts': {'created_at':'TEXT','updated_at':'TEXT','publication_status':"TEXT DEFAULT 'published'",'subject_id':'INTEGER','topic_id':'INTEGER','kind':"TEXT DEFAULT 'work'",'tags':"TEXT DEFAULT ''",'solution':"TEXT DEFAULT ''",'goal':"TEXT DEFAULT ''",'questions':"TEXT DEFAULT ''",'deleted_at':'TEXT','section':"TEXT NOT NULL DEFAULT ''"},
            'comments': {'created_at':'TEXT','updated_at':'TEXT','deleted': 'INTEGER DEFAULT 0'},
            'reports': {'status':"TEXT DEFAULT 'open'",'resolution':"TEXT DEFAULT ''",'kind':"TEXT NOT NULL DEFAULT 'complaint'",'created_at':'TEXT','resolved_at':'TEXT','resolved_by':'TEXT','outcome':"TEXT DEFAULT ''",'post_title':"TEXT DEFAULT ''"}}
        for table,fields in additions.items():
            existing={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
            for col,decl in fields.items():
                if col not in existing: c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subjects(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,description TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY,subject_id INTEGER NOT NULL REFERENCES subjects(id),name TEXT NOT NULL,description TEXT NOT NULL,UNIQUE(subject_id,name));
        CREATE TABLE IF NOT EXISTS collections(id INTEGER PRIMARY KEY,author TEXT NOT NULL REFERENCES users(username),title TEXT NOT NULL,description TEXT DEFAULT '',subject_id INTEGER REFERENCES subjects(id),created_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS collection_items(collection_id INTEGER REFERENCES collections(id),post_id INTEGER REFERENCES posts(id),position INTEGER NOT NULL,PRIMARY KEY(collection_id,post_id));
        CREATE TABLE IF NOT EXISTS xp_events(id INTEGER PRIMARY KEY,username TEXT REFERENCES users(username),event_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,amount INTEGER NOT NULL,created_at TEXT NOT NULL,reversed_at TEXT);
        CREATE TABLE IF NOT EXISTS reactions(username TEXT REFERENCES users(username),post_id INTEGER REFERENCES posts(id),PRIMARY KEY(username,post_id));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT,action TEXT,entity TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS submissions(username TEXT,token TEXT,result TEXT,PRIMARY KEY(username,token));
        CREATE TABLE IF NOT EXISTS login_attempts(identity TEXT PRIMARY KEY,count INTEGER,started REAL);
        CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS post_tags(post_id INTEGER REFERENCES posts(id),tag_id INTEGER REFERENCES tags(id),PRIMARY KEY(post_id,tag_id));
        ''')
        if 'details' not in {r[1] for r in c.execute('PRAGMA table_info(audit)')}:
            c.execute("ALTER TABLE audit ADD COLUMN details TEXT DEFAULT '{}'")
        c.execute("UPDATE reports SET post_title=coalesce((SELECT title FROM posts WHERE posts.id=reports.post_id),'Публикация недоступна') WHERE post_title=''")
        c.execute('INSERT OR IGNORE INTO migrations VALUES(2,?)',(now(),))
        if not c.execute('SELECT 1 FROM migrations WHERE version=3').fetchone():
            # Rebuild atomically to allow a user report without a fictional post_id.
            c.execute('''CREATE TABLE reports_v3 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER,reporter TEXT NOT NULL,reason TEXT NOT NULL,
                status TEXT DEFAULT 'open',resolution TEXT DEFAULT '',kind TEXT NOT NULL DEFAULT 'complaint',
                created_at TEXT,resolved_at TEXT,resolved_by TEXT,outcome TEXT DEFAULT '',post_title TEXT DEFAULT '',
                target_type TEXT NOT NULL DEFAULT 'post',comment_id INTEGER,target_username TEXT,target_excerpt TEXT DEFAULT '')''')
            original=[r[1] for r in c.execute('PRAGMA table_info(reports)')]
            fields=','.join(original)
            c.execute(f'INSERT INTO reports_v3 ({fields}) SELECT {fields} FROM reports')
            c.execute('DROP TABLE reports')
            c.execute('ALTER TABLE reports_v3 RENAME TO reports')
            c.execute('CREATE INDEX reports_target_status ON reports(target_type,status,post_id,comment_id,target_username)')
            c.execute('INSERT INTO migrations VALUES(3,?)',(now(),))
        if not c.execute('SELECT 1 FROM migrations WHERE version=1').fetchone():
            subjects={'Математика':['Алгебра','Геометрия'],'Информатика':['Алгоритмы','Программирование'],'Физика':['Механика','Квантовая физика'],'Химия':['Органическая химия'],'Биология':['Генетика'],'История':['История Казахстана'],'Литература':['Анализ произведений'],'Иностранные языки':['Английский язык'],'Искусство и фотография':['Фотография'],'Проекты и другие направления':['Совместные проекты'],'Астрономия':['Космос']}
            for name,topics in subjects.items():
                sid=c.execute('INSERT INTO subjects(name,description) VALUES(?,?)',(name,'Работы, задачи, фотографии и идеи по направлению «'+name+'».')).lastrowid
                for t in ['Без темы']+topics: c.execute('INSERT INTO topics(subject_id,name,description) VALUES(?,?,?)',(sid,t,'Материалы по теме «'+t+'».'))
            for pid,subject,topic,kind in c.execute('SELECT id,subject,subcategory,post_type FROM posts').fetchall():
                match=next((n for n in subjects if n in (subject or '')),'Проекты и другие направления')
                sid=c.execute('SELECT id FROM subjects WHERE name=?',(match,)).fetchone()[0]
                topic=topic or 'Без темы'
                c.execute('INSERT OR IGNORE INTO topics(subject_id,name,description) VALUES(?,?,?)',(sid,topic,'Существующая тема проекта.'))
                tid=c.execute('SELECT id FROM topics WHERE subject_id=? AND name=?',(sid,topic)).fetchone()[0]
                c.execute('UPDATE posts SET subject_id=?,topic_id=?,kind=? WHERE id=?',(sid,tid,{'task':'task','project':'idea','photo':'photo'}.get(kind,'work'),pid))
            # Original registration/publication dates are unknown; do not invent them.
            c.execute('INSERT INTO migrations VALUES(1,?)',(now(),))
        for username,password in c.execute('SELECT username,password FROM users').fetchall():
            if not password.startswith(('scrypt:','pbkdf2:')):
                c.execute('UPDATE users SET password=? WHERE username=?',(generate_password_hash(password),username))
    c.close()

migrate()

@app.before_request
def security():
    g.user=one('SELECT username,role,muted_until,mute_reason FROM users WHERE username=? AND deleted_at IS NULL',(session.get('username'),))
    if not g.user:session.pop('username',None)
    else:g.user['muted']=active_mute(g.user)
    session.setdefault('csrf',secrets.token_hex(24))
    if request.method not in ('GET','HEAD','OPTIONS'):
        if request.headers.get('X-CSRF-Token') != session['csrf']: return jsonify(error='Сессия формы истекла. Повторите отправку.',code='csrf_expired'),403
        origin=request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'): fail('Недопустимый источник запроса',403)
        if g.user and g.user['muted'] and request.method in ('POST','PUT','PATCH') and request.endpoint in {
            'save_post','comment','edit_comment','update_profile','collections','collection_detail','collection_items','apply','accept','application_status'}:
            fail('Публикация и изменение материалов ограничены до '+g.user['muted_until']+'. '+g.user['mute_reason'],403)

@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data: https:; style-src 'self'; style-src-attr 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    if request.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
    return response
@app.errorhandler(HTTPException)
def http_error(e):
    if request.path.startswith('/api/'): return jsonify(error=e.description),e.code
    return render_template('hub.html'),e.code

@app.get('/')
@app.get('/subjects')
@app.get('/subjects/<int:sid>')
@app.get('/topics/<int:tid>')
@app.get('/publications')
@app.get('/collections')
@app.get('/collections/<int:cid>')
@app.get('/new')
@app.get('/edit/<int:pid>')
@app.get('/my/<section>')
@app.get('/settings')
@app.get('/profile/<username>')
@app.get('/post/<int:pid>')
@app.get('/admin')
def page(**kwargs): return render_template('hub.html')

@app.get('/api/session')
def get_session(): return jsonify(user=g.user,csrf=session['csrf'],types=TYPES,config=CONFIG)

@app.post('/api/register')
def register():
    d=data(); username=text(d,'username',32,True); password=text(d,'password',128,True)
    if not re.fullmatch(r'[\w-]{3,32}',username): fail('Имя: 3–32 буквы, цифры, дефис или подчёркивание')
    if len(password)<8: fail('Пароль должен содержать минимум 8 символов')
    db().execute('BEGIN IMMEDIATE')
    if one('SELECT 1 FROM users WHERE casefold(username)=?',(username.casefold(),)): fail('Имя пользователя занято',409)
    db().execute('INSERT INTO users(username,password,role,created_at,display_name,bio) VALUES(?,?,?,?,?,?)',(username,generate_password_hash(password),'user',now(),username,'')); db().commit()
    session['username']=username;session.permanent=True
    return jsonify(ok=True),201
@app.post('/api/login')
def login():
    import time
    d=data();username=text(d,'username',32,True); password=text(d,'password',128,True)
    identity=(request.remote_addr or 'local')+':'+username.casefold(); a=one('SELECT * FROM login_attempts WHERE identity=?',(identity,))
    if a and a['count']>=10 and time.time()-a['started']<900: fail('Слишком много попыток. Повторите через 15 минут.',429)
    u=one('SELECT * FROM users WHERE username=? AND deleted_at IS NULL',(username,))
    if not u:
        matches=rows('SELECT * FROM users WHERE casefold(username)=? AND deleted_at IS NULL',(username.casefold(),))
        if len(matches)==1:u=matches[0]
    if not u or not check_password_hash(u['password'],password):
        if not a or time.time()-a['started']>=900: db().execute('INSERT OR REPLACE INTO login_attempts VALUES(?,1,?)',(identity,time.time()))
        else: db().execute('UPDATE login_attempts SET count=count+1 WHERE identity=?',(identity,))
        db().commit();fail('Неверное имя или пароль',401)
    db().execute('DELETE FROM login_attempts WHERE identity=?',(identity,));db().commit()
    session.clear();session.update(username=u['username'],csrf=secrets.token_hex(24));session.permanent=True
    return jsonify(ok=True)
@app.post('/api/logout')
def logout(): session.clear();return jsonify(ok=True)

@app.post('/api/password')
def password_change():
    username=member();d=data();current=text(d,'current_password',128,True);new=text(d,'new_password',128,True)
    u=one('SELECT password FROM users WHERE username=?',(username,))
    if not check_password_hash(u['password'],current):fail('Текущий пароль неверен',403)
    if len(new)<8:fail('Новый пароль должен содержать минимум 8 символов')
    db().execute('UPDATE users SET password=? WHERE username=?',(generate_password_hash(new),username));db().commit()
    return jsonify(ok=True)

@app.get('/api/subjects')
def subjects(): return jsonify(subjects=rows('SELECT * FROM subjects ORDER BY id'),topics=rows('SELECT * FROM topics ORDER BY id'))

def xp(username,kind,key):
    if one('SELECT 1 FROM xp_events WHERE event_key=?',(key,)): return
    amount=CONFIG['xp'][kind];cap=CONFIG['daily_caps'].get(kind)
    if cap is not None:
        used=one('SELECT coalesce(sum(amount),0) AS n FROM xp_events WHERE username=? AND kind=? AND substr(created_at,1,10)=?',(username,kind,now()[:10]))['n']
        amount=max(0,min(amount,cap-used))
    db().execute('INSERT OR IGNORE INTO xp_events(username,event_key,kind,amount,created_at) VALUES(?,?,?,?,?)',(username,key,kind,amount,now()))
def reverse(key): db().execute('UPDATE xp_events SET reversed_at=? WHERE event_key=? AND reversed_at IS NULL',(now(),key))
def enrich(p):
    p.update(one('''SELECT u.deleted_at IS NOT NULL AS author_deleted,s.name AS subject_name,t.name AS topic_name,
        (SELECT count(*) FROM comments WHERE post_id=p.id AND deleted=0) AS comment_count,
        EXISTS(SELECT 1 FROM bookmarks WHERE username=? AND post_id=p.id) AS saved,
        (SELECT count(*) FROM reactions WHERE post_id=p.id) AS helpful
        FROM posts p LEFT JOIN users u ON u.username=p.author LEFT JOIN subjects s ON s.id=p.subject_id
        LEFT JOIN topics t ON t.id=p.topic_id WHERE p.id=?''',(session.get('username'),p['id'])))
    p['author_deleted']=bool(p['author_deleted']);p['saved']=bool(p['saved'])
    return p
@app.get('/api/posts')
def posts():
    where=['p.deleted_at IS NULL'];args=[]
    scope=request.args.get('scope')
    if scope in ('mine','drafts','saved'):
        username=member()
        if scope=='saved': where+=['p.id IN (SELECT post_id FROM bookmarks WHERE username=?)',"p.publication_status='published'"];args.append(username)
        else: where+=['p.author=?','p.publication_status=?'];args += [username,'draft' if scope=='drafts' else 'published']
    else: where.append("p.publication_status='published'")
    for field in ('subject_id','topic_id','kind','author'):
        if request.args.get(field): where.append('p.'+field+'=?');args.append(request.args[field])
    q=request.args.get('q','').strip()
    if q:
        where.append("(casefold(p.title) LIKE ? ESCAPE '!' OR casefold(p.content) LIKE ? ESCAPE '!' OR casefold(p.abstract) LIKE ? ESCAPE '!' OR casefold(p.tags) LIKE ? ESCAPE '!' OR casefold(p.section) LIKE ? ESCAPE '!')")
        pattern='%'+q.casefold().replace('!','!!').replace('%','!%').replace('_','!_')+'%';args.extend([pattern]*5)
    order={'new':'p.id DESC','old':'p.id ASC','discussed':'(SELECT count(*) FROM comments c WHERE c.post_id=p.id AND c.deleted=0) DESC,p.id DESC'}.get(request.args.get('sort'),'p.id DESC')
    items=rows('SELECT p.* FROM posts p WHERE '+' AND '.join(where)+' ORDER BY '+order,args)
    return jsonify(items=[enrich(p) for p in items],total=len(items))
@app.get('/api/posts/<int:pid>')
def get_post(pid):
    p=post(pid)
    comments=rows('SELECT c.*,u.deleted_at IS NOT NULL AS author_deleted FROM comments c LEFT JOIN users u ON u.username=c.author WHERE post_id=? ORDER BY c.id',(pid,))
    for c in comments:
        if c['deleted']:c.update(text='',image='',filepath='',filename='')
    return jsonify(post=enrich(p),comments=comments)

def upload(field):
    f=request.files.get(field)
    if not f or not f.filename:return None
    content=f.read(10*1024*1024+1)
    if len(content)>10*1024*1024: fail('Размер файла не должен превышать 10 МБ')
    ext=Path(f.filename).suffix.lower()
    if ext not in ('.png','.jpg','.jpeg','.webp','.pdf','.txt'): fail('Разрешены PNG, JPG, WEBP, PDF и TXT')
    if field in ('cover','avatar','comment_image') and ext not in ('.png','.jpg','.jpeg','.webp'): fail('Выберите изображение PNG, JPG или WEBP')
    if ext in ('.png','.jpg','.jpeg','.webp'):
        from PIL import Image,UnidentifiedImageError
        import io
        try:
            with Image.open(io.BytesIO(content)) as im:
                if im.width*im.height>20000000: fail('Изображение слишком большое: максимум 20 мегапикселей')
                im.load();output=io.BytesIO();im.convert('RGB').save(output,format='JPEG',quality=90);content=output.getvalue();ext='.jpg'
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError): fail('Не удалось прочитать изображение')
    elif ext=='.pdf' and not content.startswith(b'%PDF-'): fail('Некорректный PDF')
    elif ext=='.txt':
        try: content.decode('utf-8')
        except UnicodeDecodeError: fail('Текстовый файл должен быть в UTF-8')
    name=uuid.uuid4().hex+ext;(UPLOADS/name).write_bytes(content)
    return {'filename':Path(f.filename).name,'url':'/uploads/'+name}

@app.get('/uploads/<path:filename>')
def downloads(filename):
    if Path(filename).name!=filename: fail('Файл не найден',404)
    url='/uploads/'+filename
    refs=rows('SELECT author,publication_status,deleted_at FROM posts WHERE image=? OR filepath=? OR solution_filepath=? OR filename=? OR solution_filename=?',(url,url,url,filename,filename))
    avatar=one('SELECT 1 FROM users WHERE avatar=?',(url,))
    comment_ref=one("SELECT 1 FROM comments c JOIN posts p ON p.id=c.post_id WHERE c.deleted=0 AND p.deleted_at IS NULL AND p.publication_status='published' AND (c.filename=? OR c.filepath=? OR c.image=?)",(filename,url,url))
    if not avatar and not comment_ref and not any(not p['deleted_at'] and (p['publication_status']=='published' or p['author']==session.get('username')) for p in refs):
        fail('Файл не найден',404)
    return send_from_directory(UPLOADS,filename,as_attachment=Path(filename).suffix.lower() not in ('.jpg','.jpeg','.png','.webp'),download_name=filename)

def duplicate():
    token=request.headers.get('Idempotency-Key','')
    if not token or len(token)>100: fail('Отсутствует идентификатор отправки. Обновите форму.')
    old=one('SELECT result FROM submissions WHERE username=? AND token=?',(member(),token))
    return token,json.loads(old['result']) if old else None
def completed(token,result):
    db().execute('INSERT INTO submissions VALUES(?,?,?)',(member(),token,json.dumps(result)));db().commit();return jsonify(result)

@app.route('/api/posts',methods=['POST'])
@app.route('/api/posts/<int:pid>',methods=['PUT'])
def save_post(pid=None):
    username=member();db().execute('BEGIN IMMEDIATE');token,old=duplicate()
    if old:return jsonify(old)
    p=post(pid) if pid else None
    if p:manage_post(p)
    d=data();title=text(d,'title',180,True);content=text(d,'content',100000);kind=text(d,'kind',20);status=text(d,'publication_status',20) or 'published'
    if kind not in TYPES or status not in ('draft','published'):fail('Неверный тип или статус')
    if p and p['publication_status']=='published' and status=='draft':fail('Опубликованный материал нельзя скрыть как черновик')
    if status=='published' and not content and kind!='photo':fail('Добавьте содержание публикации')
    sid=integer(d.get('subject_id'));tid=integer(d.get('topic_id'))
    t=one('SELECT * FROM topics WHERE id=? AND subject_id=?',(tid,sid))
    if not t:fail('Тема должна относиться к выбранному предмету')
    if kind=='photo' and status=='published' and not request.files.get('cover') and not (p and p['image']):fail('Добавьте фотографию')
    fields={'title':title,'content':content,'kind':kind,'publication_status':status,'subject_id':sid,'topic_id':tid,'subject':one('SELECT name FROM subjects WHERE id=?',(sid,))['name'],'subcategory':t['name'],'updated_at':now(),
        'section':text(d,'section',120) if 'section' in d else (p.get('section','') if p else '')}
    for key in ('tags','hint','solution','goal','questions','difficulty','references_list','abstract','methodology','coauthors','book_author','volume','roles_needed','grade_level','olympiad_name','book_reference'):
        fields[key]=text(d,key,10000) if key in d else (p.get(key,'') if p else '')
    cover=upload('cover');file=upload('file');sol=upload('solution_file')
    if cover:fields['image']=cover['url']
    if file:fields.update(filename=file['filename'],filepath=file['url'])
    if sol:fields.update(solution_filename=sol['filename'],solution_filepath=sol['url'])
    if p:
        db().execute('UPDATE posts SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[pid])
        if p['author'] != username:
            audit('admin_edit_post',pid,{'author':p['author'],'title':p['title'],
                'previous':{k:p.get(k) for k in fields if k!='updated_at' and p.get(k)!=fields[k]}})
    else:
        fields.update(author=username,category=TYPES[kind],post_type={'work':'research','idea':'project'}.get(kind,kind),created_at=now())
        pid=db().execute('INSERT INTO posts('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',list(fields.values())).lastrowid
    db().execute('DELETE FROM post_tags WHERE post_id=?',(pid,))
    for tag in {v.strip()[:40] for v in fields['tags'].split(',') if v.strip()}:
        db().execute('INSERT OR IGNORE INTO tags(name) VALUES(?)',(tag,));db().execute('INSERT INTO post_tags SELECT ?,id FROM tags WHERE name=?',(pid,tag))
    if status=='published':xp(p['author'] if p else username,'publication','post:'+str(pid))
    return completed(token,{'id':pid})

@app.delete('/api/posts/<int:pid>/delete')
def delete_post(pid):
    db().execute('BEGIN IMMEDIATE')
    p=post(pid);manage_post(p)
    if p['author'] != member():audit('admin_delete_post',pid,{'author':p['author'],'title':p['title']})
    remove_post(p);db().commit();return jsonify(ok=True)
def remove_post(p):
    db().execute('UPDATE posts SET deleted_at=? WHERE id=?',(now(),p['id']));reverse('post:'+str(p['id']))
    db().execute("UPDATE reports SET status='resolved',outcome='deleted',resolved_at=?,resolved_by=?,resolution='Публикация удалена. Заявление закрыто.' WHERE post_id=? AND status='open'",(now(),member(),p['id']))
    db().execute("UPDATE xp_events SET reversed_at=? WHERE event_key LIKE ? AND reversed_at IS NULL",(now(),'helpful:'+str(p['id'])+':%'))
    for item in rows('SELECT i.collection_id FROM collection_items i JOIN collections c ON c.id=i.collection_id WHERE i.post_id=? AND c.deleted_at IS NULL',(p['id'],)):check_collection_xp(item['collection_id'])

@app.post('/api/posts/<int:pid>/comments')
def comment(pid):
    p=post(pid);username=member()
    if p['publication_status']!='published':fail('Комментарии доступны после публикации')
    db().execute('BEGIN IMMEDIATE');token,old=duplicate()
    if old:return jsonify(old)
    d=data();value=text(d,'text',5000);parent=d.get('parent_id') or None
    if not value and not request.files.get('comment_image'):fail('Добавьте текст или изображение комментария')
    if parent and not one('SELECT 1 FROM comments WHERE id=? AND post_id=?',(integer(parent),pid)):fail('Комментарий для ответа не найден')
    img=upload('comment_image')
    cid=db().execute('INSERT INTO comments(post_id,parent_id,author,text,image,created_at) VALUES(?,?,?,?,?,?)',(pid,parent,username,value,img['url'] if img else '',now())).lastrowid
    return completed(token,{'id':cid})
@app.route('/api/comments/<int:cid>',methods=['PUT','DELETE'])
def edit_comment(cid):
    c=one('SELECT * FROM comments WHERE id=?',(cid,))
    if not c:fail('Комментарий не найден',404)
    member();post(c['post_id'])
    if request.method=='DELETE':
        if g.user['role']!='admin':owner(c)
        remove_comment(c)
    else:
        owner(c)
        if c['deleted']:fail('Комментарий удалён',410)
        value=text(data(),'text',5000)
        if not value and not c['image']:fail('Добавьте текст комментария')
        db().execute('UPDATE comments SET text=?,updated_at=? WHERE id=?',(value,now(),cid))
    db().commit();return jsonify(ok=True)

def remove_comment(c):
    if c['deleted']:return
    db().execute("UPDATE comments SET text='',image='',filename='',filepath='',deleted=1,is_accepted=0,updated_at=? WHERE id=?",(now(),c['id']))
    if c['is_accepted']:db().execute("UPDATE posts SET status='❓ В поиске решения' WHERE id=?",(c['post_id'],))
    db().execute("UPDATE reports SET status='resolved',outcome='deleted',resolved_at=?,resolved_by=?,resolution='Комментарий удалён.' WHERE target_type='comment' AND comment_id=? AND status='open'",(now(),member(),c['id']))
    if c['author']!=member():audit('admin_delete_comment',c['id'],{'author':c['author'],'post_id':c['post_id']})
@app.post('/api/comments/<int:cid>/accept')
def accept(cid):
    c=one('SELECT * FROM comments WHERE id=? AND deleted=0',(cid,))
    if not c:fail('Комментарий не найден',404)
    p=post(c['post_id']);owner(p)
    db().execute('UPDATE comments SET is_accepted=0 WHERE post_id=?',(p['id'],));db().execute('UPDATE comments SET is_accepted=1 WHERE id=?',(cid,));db().execute("UPDATE posts SET status='✅ Решено' WHERE id=?",(p['id'],));db().commit();return jsonify(ok=True)

@app.post('/api/bookmarks/toggle')
def bookmark():
    username=member();pid=integer(data().get('post_id'));post(pid)
    if one('SELECT 1 FROM bookmarks WHERE username=? AND post_id=?',(username,pid)):db().execute('DELETE FROM bookmarks WHERE username=? AND post_id=?',(username,pid))
    else:db().execute('INSERT INTO bookmarks VALUES(?,?)',(username,pid))
    db().commit();return jsonify(ok=True)
@app.post('/api/posts/<int:pid>/like')
def helpful(pid):
    username=member();p=post(pid)
    if p['author']==username:fail('Нельзя отметить полезность собственной публикации')
    if p['publication_status']!='published':fail('Материал ещё не опубликован')
    db().execute('BEGIN IMMEDIATE')
    db().execute('INSERT OR IGNORE INTO reactions VALUES(?,?)',(username,pid))
    xp(p['author'],'helpful',f'helpful:{pid}:{username}');db().commit();return jsonify(ok=True)

def collection(cid):
    c=one('SELECT * FROM collections WHERE id=? AND deleted_at IS NULL',(cid,))
    if not c:fail('Сборник не найден',404)
    c['author_deleted']=bool(one('SELECT 1 FROM users WHERE username=? AND deleted_at IS NOT NULL',(c['author'],)))
    return c
def check_collection_xp(cid):
    c=collection(cid)
    n=one("SELECT count(*) n FROM collection_items i JOIN posts p ON i.post_id=p.id WHERE i.collection_id=? AND p.deleted_at IS NULL AND p.publication_status='published'",(cid,))['n']
    if n>=2 and c['description'].strip():xp(c['author'],'collection','collection:'+str(cid))
    else:reverse('collection:'+str(cid))
@app.route('/api/collections',methods=['GET','POST'])
def collections():
    if request.method=='GET':
        author=request.args.get('author');q='SELECT c.*,(SELECT deleted_at IS NOT NULL FROM users WHERE username=c.author) author_deleted,(SELECT count(*) FROM collection_items WHERE collection_id=c.id) count FROM collections c WHERE deleted_at IS NULL';args=[]
        if request.args.get('mine'):author=member()
        if author:q+=' AND author=?';args.append(author)
        return jsonify(items=rows(q+' ORDER BY id DESC',args))
    username=member();db().execute('BEGIN IMMEDIATE');token,old=duplicate()
    if old:return jsonify(old)
    d=data();sid=integer(d.get('subject_id'))
    if not one('SELECT 1 FROM subjects WHERE id=?',(sid,)):fail('Предмет не найден')
    cid=db().execute('INSERT INTO collections(author,title,description,subject_id,created_at) VALUES(?,?,?,?,?)',(username,text(d,'title',180,True),text(d,'description',10000),sid,now())).lastrowid
    return completed(token,{'id':cid})
@app.route('/api/collections/<int:cid>',methods=['GET','PUT','DELETE'])
def collection_detail(cid):
    c=collection(cid)
    if request.method=='GET':
        items=[]
        for i in rows('SELECT post_id,position FROM collection_items WHERE collection_id=? ORDER BY position',(cid,)):
            p=one("SELECT * FROM posts WHERE id=? AND deleted_at IS NULL AND publication_status='published'",(i['post_id'],))
            items.append({'post_id':i['post_id'],'post':enrich(p) if p else None})
        return jsonify(collection=c,items=items)
    owner(c)
    if request.method=='DELETE':db().execute('UPDATE collections SET deleted_at=? WHERE id=?',(now(),cid));reverse('collection:'+str(cid))
    else:
        d=data();db().execute('UPDATE collections SET title=?,description=? WHERE id=?',(text(d,'title',180,True),text(d,'description',10000),cid));check_collection_xp(cid)
    db().commit();return jsonify(ok=True)
@app.route('/api/collections/<int:cid>/items',methods=['PUT'])
def collection_items(cid):
    owner(collection(cid));ids=data().get('post_ids')
    if not isinstance(ids,list) or len(ids)>500:fail('Некорректный список материалов')
    ids=[integer(v) for v in ids]
    if len(set(ids))!=len(ids):fail('Повторяющиеся материалы')
    existing={r['post_id'] for r in rows('SELECT post_id FROM collection_items WHERE collection_id=?',(cid,))}
    for pid in ids:
        if pid not in existing and post(pid)['publication_status']!='published':fail('Нельзя добавить черновик')
    db().execute('DELETE FROM collection_items WHERE collection_id=?',(cid,))
    for position,pid in enumerate(ids):db().execute('INSERT INTO collection_items VALUES(?,?,?)',(cid,pid,position))
    check_collection_xp(cid);db().commit();return jsonify(ok=True)

@app.get('/api/profile/<username>')
def profile(username):
    u=one('SELECT username,display_name,bio,school,achievements,badge,avatar,created_at,role FROM users WHERE username=? AND deleted_at IS NULL',(username,))
    if not u:fail('Профиль не найден',404)
    u['days_on_site']=account_age(u['created_at'])
    total=one('SELECT coalesce(sum(amount),0) n FROM xp_events WHERE username=? AND reversed_at IS NULL',(username,))['n']
    levels=CONFIG['levels'];index=max(i for i,v in enumerate(levels) if total>=v[0]);next_level=levels[index+1][0] if index+1<len(levels) else None
    u.update(xp=total,level_title=levels[index][1],level_start=levels[index][0],next_level=next_level,
        posts_count=one("SELECT count(*) n FROM posts WHERE author=? AND deleted_at IS NULL AND publication_status='published'",(username,))['n'],
        collections_count=one('SELECT count(*) n FROM collections WHERE author=? AND deleted_at IS NULL',(username,))['n'],
        comments_count=one('SELECT count(*) n FROM comments WHERE author=? AND deleted=0',(username,))['n'])
    if username==session.get('username'):u['events']=rows('SELECT kind,amount,created_at,reversed_at FROM xp_events WHERE username=? ORDER BY id DESC',(username,))
    return jsonify(u)
@app.post('/api/profile/update')
def update_profile():
    username=member();d=data();fields={k:text(d,k,2000) for k in ('display_name','bio','school','achievements')};avatar=upload('avatar')
    if avatar:fields['avatar']=avatar['url']
    db().execute('UPDATE users SET '+','.join(k+'=?' for k in fields)+' WHERE username=?',list(fields.values())+[username])
    if fields['display_name'] and fields['bio']:xp(username,'profile','profile:'+username)
    db().commit();return jsonify(ok=True)

@app.post('/api/posts/<int:pid>/apply')
def apply(pid):
    username=member();p=post(pid);d=data()
    if not p['roles_needed']:fail('Набор в команду не открыт')
    if p['author']==username:fail('Вы автор проекта')
    if one('SELECT 1 FROM team_applications WHERE post_id=? AND applicant=?',(pid,username)):fail('Заявка уже отправлена',409)
    db().execute('INSERT INTO team_applications(post_id,applicant,role_applied,message) VALUES(?,?,?,?)',(pid,username,text(d,'role_applied',100,True),text(d,'message',3000,True)));db().commit();return jsonify(ok=True)
@app.get('/api/applications')
def applications():return jsonify(items=rows('SELECT a.*,p.title FROM team_applications a JOIN posts p ON a.post_id=p.id WHERE p.author=?',(member(),)))
@app.put('/api/applications/<int:aid>')
def application_status(aid):
    a=one('SELECT * FROM team_applications WHERE id=?',(aid,))
    if not a:fail('Заявка не найдена',404)
    owner(post(a['post_id']));status=data().get('status')
    if status not in ('accepted','rejected'):fail('Неверный статус')
    db().execute('UPDATE team_applications SET status=? WHERE id=?',(status,aid));db().commit();return jsonify(ok=True)

@app.route('/api/reports',methods=['GET','POST'])
def reports():
    if request.method=='GET':
        staff()
        items=rows('SELECT r.*,p.deleted_at,p.author AS post_author FROM reports r LEFT JOIN posts p ON p.id=r.post_id ORDER BY r.id DESC')
        if g.user['role']!='admin':items=[r for r in items if r['kind']=='complaint' and r['target_type']=='post']
        return jsonify(items=items)
    username=member();d=data();kind=d.get('kind','complaint');target=d.get('target_type','post')
    if kind not in ('complaint','deletion') or target not in ('post','comment','user'):fail('Неизвестный тип обращения')
    if kind=='deletion' and target!='post':fail('Заявление на удаление относится к публикации')
    reason=text(d,'reason',2000,True)
    db().execute('BEGIN IMMEDIATE');pid=None;cid=None;target_username=None;excerpt=''
    if target=='user':
        target_username=text(d,'target_username',32,True)
        u=one('SELECT username FROM users WHERE username=? AND deleted_at IS NULL',(target_username,))
        if not u:fail('Пользователь не найден',404)
        title='Пользователь '+target_username
    else:
        if target=='comment':
            cid=integer(d.get('comment_id'));c=one('SELECT * FROM comments WHERE id=? AND deleted=0',(cid,))
            if not c:fail('Комментарий не найден',404)
            pid=c['post_id'];excerpt=c['text'] or 'Комментарий с изображением'
        else:pid=integer(d.get('post_id'))
        p=post(pid)
        if p['publication_status']!='published':fail('Обращения доступны только для опубликованных материалов')
        title=p['title'] if target=='post' else 'Комментарий '+c['author']+' к «'+p['title']+'»'
    existing=one("SELECT id FROM reports WHERE reporter=? AND kind=? AND target_type=? AND post_id IS ? AND comment_id IS ? AND target_username IS ? AND status='open'",(username,kind,target,pid,cid,target_username))
    if existing:fail('Такое обращение уже ожидает рассмотрения. Его статус доступен в меню аккаунта.',409)
    rid=db().execute('INSERT INTO reports(post_id,reporter,reason,kind,created_at,post_title,target_type,comment_id,target_username,target_excerpt) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (pid,username,reason,kind,now(),title,target,cid,target_username,excerpt)).lastrowid
    db().commit();return jsonify(ok=True,id=rid)

@app.get('/api/my/reports')
def my_reports():
    return jsonify(items=rows('SELECT * FROM reports WHERE reporter=? ORDER BY id DESC',(member(),)))

@app.put('/api/reports/<int:rid>')
def resolve_report(rid):
    staff();db().execute('BEGIN IMMEDIATE');r=one('SELECT * FROM reports WHERE id=?',(rid,))
    if not r:fail('Жалоба не найдена',404)
    if r['kind']=='deletion' or r['target_type']!='post':staff(True)
    if r['status']!='open':fail('Обращение уже рассмотрено. Обновите список.',409)
    d=data();resolution=text(d,'resolution',2000,True)
    remove=d.get('remove',False)
    if not isinstance(remove,bool):fail('Неверный формат решения')
    action=d.get('action') or ('delete' if remove else 'keep')
    if action not in ('keep','delete','mute'):fail('Неизвестное действие')
    if action=='mute' and r['target_type']!='user':fail('Мут применяется только к пользователю')
    if action=='delete':
        if r['target_type']=='post':
            p=one('SELECT * FROM posts WHERE id=? AND deleted_at IS NULL',(r['post_id'],))
            if p:remove_post(p)
        elif r['target_type']=='comment':
            c=one('SELECT * FROM comments WHERE id=?',(r['comment_id'],))
            if c:remove_comment(c)
        else:delete_account(r['target_username'])
    elif action=='mute':mute_account(r['target_username'],d)
    outcome={'keep':'kept','delete':'deleted','mute':'muted'}[action]
    db().execute("UPDATE reports SET status='resolved',resolution=?,outcome=?,resolved_at=?,resolved_by=? WHERE id=?",
        (resolution,outcome,now(),member(),rid))
    audit('resolve_report',rid,{'target_type':r['target_type'],'kind':r['kind'],'post_id':r['post_id'],'outcome':outcome,'resolution':resolution})
    db().commit();return jsonify(ok=True)


def moderated_user(username):
    staff(True)
    u=one('SELECT username,role FROM users WHERE username=? AND deleted_at IS NULL',(username,))
    if not u:fail('Пользователь не найден',404)
    if username==member() or u['role']=='admin':fail('Нельзя применить это действие к себе или администратору',403)
    return u


def mute_account(username,d):
    moderated_user(username)
    if isinstance(d.get('minutes'),bool) or not re.fullmatch(r'\d+',str(d.get('minutes'))):fail('Срок должен быть целым числом минут')
    minutes=integer(d.get('minutes'))
    if minutes<0 or minutes>5256000:fail('Срок: от 1 минуты до 10 лет; 0 — снять мут')
    reason=text(d,'reason',1000) or text(d,'resolution',2000)
    until=(datetime.now(timezone.utc)+timedelta(minutes=minutes)).isoformat(timespec='seconds') if minutes else None
    db().execute('UPDATE users SET muted_until=?,mute_reason=? WHERE username=?',(until,reason if minutes else '',username))
    audit('mute_user' if minutes else 'unmute_user',username,{'until':until,'reason':reason})
    return until


def delete_account(username):
    moderated_user(username)
    db().execute("UPDATE users SET deleted_at=?,password=?,display_name='Удалённый пользователь',bio='',school='',achievements='',badge='',avatar='',muted_until=NULL,mute_reason='' WHERE username=?",
        (now(),generate_password_hash(secrets.token_hex(32)),username))
    db().execute('DELETE FROM bookmarks WHERE username=?',(username,))
    db().execute("UPDATE reports SET status='resolved',outcome='deleted',resolved_at=?,resolved_by=?,resolution='Аккаунт удалён.' WHERE target_type='user' AND target_username=? AND status='open'",(now(),member(),username))
    audit('delete_user',username,{'public_materials':'preserved'})


@app.put('/api/admin/users/<username>/mute')
def mute_user(username):
    staff(True);db().execute('BEGIN IMMEDIATE');until=mute_account(username,data());db().commit();return jsonify(ok=True,muted_until=until)


@app.delete('/api/admin/users/<username>')
def delete_user(username):
    staff(True);db().execute('BEGIN IMMEDIATE');delete_account(username);db().commit();return jsonify(ok=True)

@app.get('/api/admin')
def admin():
    staff(True);return jsonify(users=rows('SELECT username,role,muted_until,mute_reason,created_at FROM users WHERE deleted_at IS NULL'),audit=rows('SELECT * FROM audit ORDER BY id DESC LIMIT 100'))
@app.put('/api/admin/role')
def role():
    staff(True);d=data();name=text(d,'username',32,True);role=d.get('role')
    if role not in ('user','moderator','admin'):fail('Неизвестная роль')
    if not one('SELECT 1 FROM users WHERE username=? AND deleted_at IS NULL',(name,)):fail('Пользователь не найден',404)
    if name==member():fail('Свою роль изменить нельзя')
    db().execute('UPDATE users SET role=? WHERE username=?',(role,name));audit('role:'+role,name);db().commit();return jsonify(ok=True)
@app.post('/api/admin/taxonomy')
def taxonomy():
    staff(True);d=data();name=text(d,'name',100,True);description=text(d,'description',2000,True);sid=d.get('subject_id')
    try:
        if sid:
            if not one('SELECT 1 FROM subjects WHERE id=?',(integer(sid),)):fail('Предмет не найден')
            db().execute('INSERT INTO topics(subject_id,name,description) VALUES(?,?,?)',(sid,name,description))
        else:
            sid=db().execute('INSERT INTO subjects(name,description) VALUES(?,?)',(name,description)).lastrowid
            db().execute('INSERT INTO topics(subject_id,name,description) VALUES(?,?,?)',(sid,'Без темы','Материалы без выбранной темы.'))
    except sqlite3.IntegrityError:fail('Такое название уже существует',409)
    audit('taxonomy',name);db().commit();return jsonify(ok=True)
