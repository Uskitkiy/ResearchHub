"""End-to-end API scenarios; isolated temporary data, never production database."""
import os,tempfile,pathlib,unittest,io,uuid,sqlite3
TMP=pathlib.Path(__file__).parent / ('test-data-'+uuid.uuid4().hex)
TMP.mkdir()
os.environ['HUB_DATABASE']=str(TMP/'test.db')
os.environ['HUB_UPLOADS']=str(TMP/'uploads')
from hub import app,DB,migrate
from PIL import Image

class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING']=True
        cls.a=app.test_client();cls.b=app.test_client();cls.guest=app.test_client();cls.admin=app.test_client();cls.mod=app.test_client()
        for client,name in [(cls.a,'alice'),(cls.b,'bob'),(cls.admin,'administrator'),(cls.mod,'moderator')]:
            assert cls.req(client,'/api/register',payload={'username':name,'password':'A-test-password-123'}).status_code==201
        with sqlite3.connect(DB) as c:
            c.execute("UPDATE users SET role='admin' WHERE username='administrator'")
            c.execute("UPDATE users SET role='moderator' WHERE username='moderator'")
        catalog=cls.a.get('/api/subjects').json;cls.sid=catalog['subjects'][0]['id'];cls.tid=catalog['topics'][0]['id']
    @staticmethod
    def req(client,url,method='POST',payload=None,token=None,multipart=False):
        csrf=client.get('/api/session').json['csrf'];h={'X-CSRF-Token':csrf,'Idempotency-Key':token or str(uuid.uuid4())}
        return client.open(url,method=method,headers=h,**({'data':payload} if multipart else {'json':payload}))
    def create(self,status='published',kind='work',token=None):
        body=dict(title='Геометрия треугольника',content='Условие и анализ',subject_id=self.sid,topic_id=self.tid,kind=kind,publication_status=status)
        r=self.req(self.a,'/api/posts',payload=body,token=token)
        self.assertEqual(r.status_code,200,r.json);return r.json['id']
    def test_01_guest_and_catalog(self):
        self.assertEqual(self.guest.get('/').status_code,200)
        self.assertGreaterEqual(len(self.guest.get('/api/subjects').json['subjects']),10)
        self.assertEqual(self.req(self.guest,'/api/posts',payload={}).status_code,401)
    def test_02_auth_password_sessions(self):
        with sqlite3.connect(DB) as c:self.assertTrue(c.execute("SELECT password FROM users WHERE username='alice'").fetchone()[0].startswith('scrypt:'))
        c=app.test_client();self.assertEqual(self.req(c,'/api/login',payload={'username':'alice','password':'wrong'}).status_code,401)
        self.assertEqual(self.req(c,'/api/login',payload={'username':'alice','password':'A-test-password-123'}).status_code,200)
        self.assertEqual(c.get('/api/session').json['user']['username'],'alice')
        self.assertEqual(c.post('/api/logout').status_code,403)
        self.assertEqual(self.req(c,'/api/logout').status_code,200)
    def test_03_drafts_edit_and_rights(self):
        pid=self.create('draft');self.assertEqual(self.b.get(f'/api/posts/{pid}').status_code,404)
        self.assertNotIn(pid,[p['id'] for p in self.guest.get('/api/posts').json['items']])
        body=dict(title='Опубликованная задача',content='Дано',subject_id=self.sid,topic_id=self.tid,kind='task',publication_status='published')
        self.assertEqual(self.req(self.a,f'/api/posts/{pid}','PUT',body).status_code,200)
        self.assertEqual(self.req(self.b,f'/api/posts/{pid}','PUT',body).status_code,403)
        self.assertEqual(self.req(self.b,f'/api/posts/{pid}/delete','DELETE',{'username':'alice'}).status_code,403)
    def test_04_idempotency_xp(self):
        token=str(uuid.uuid4());p=self.create(token=token);self.assertEqual(p,self.create(token=token))
        with sqlite3.connect(DB) as c:self.assertEqual(c.execute('SELECT count(*) FROM xp_events WHERE event_key=?',('post:'+str(p),)).fetchone()[0],1)
        self.assertEqual(self.req(self.a,f'/api/posts/{p}/like').status_code,400)
        for _ in range(2):self.assertEqual(self.req(self.b,f'/api/posts/{p}/like').status_code,200)
        with sqlite3.connect(DB) as c:self.assertEqual(c.execute('SELECT count(*) FROM xp_events WHERE event_key=?',(f'helpful:{p}:bob',)).fetchone()[0],1)
    def test_05_comments_replies_tombstone(self):
        p=self.create();url=f'/api/posts/{p}/comments';token=str(uuid.uuid4())
        for value in [' ','a'*5001]:self.assertEqual(self.req(self.a,url,payload={'text':value}).status_code,400)
        cid=self.req(self.a,url,payload={'text':'<script>alert(1)</script>'},token=token).json['id']
        self.assertEqual(self.req(self.a,url,payload={'text':'same'},token=token).json['id'],cid)
        self.assertEqual(self.req(self.b,url,payload={'text':'Ответ','parent_id':cid}).status_code,200)
        self.assertEqual(self.req(self.b,f'/api/comments/{cid}','PUT',{'text':'чужое'}).status_code,403)
        self.assertEqual(self.req(self.a,f'/api/comments/{cid}','PUT',{'text':'Изменено'}).status_code,200)
        self.assertEqual(self.req(self.a,f'/api/comments/{cid}','DELETE').status_code,200)
        comments=self.a.get(f'/api/posts/{p}').json['comments'];self.assertEqual(len(comments),2);self.assertEqual(comments[0]['deleted'],1)
    def test_06_uploads_and_draft_privacy(self):
        im=io.BytesIO();Image.new('RGB',(20,20),'blue').save(im,'PNG');im.seek(0)
        d=dict(title='Фото',kind='photo',content='',subject_id=str(self.sid),topic_id=str(self.tid),publication_status='draft',cover=(im,'../../image.png'))
        r=self.req(self.a,'/api/posts',payload=d,multipart=True);self.assertEqual(r.status_code,200,r.json)
        p=self.a.get('/api/posts/'+str(r.json['id'])).json['post'];self.assertTrue(p['image'].endswith('.jpg'))
        self.assertEqual(self.a.get(p['image']).status_code,200);self.assertEqual(self.b.get(p['image']).status_code,404)
        d['cover']=(io.BytesIO(b'fake'),'bad.png');self.assertEqual(self.req(self.a,'/api/posts',payload=d,multipart=True).status_code,400)
        d['cover']=(io.BytesIO(b'code'),'evil.html');self.assertEqual(self.req(self.a,'/api/posts',payload=d,multipart=True).status_code,400)
    def test_07_collections_order_remove_and_xp(self):
        p,q=self.create(),self.create();cid=self.req(self.a,'/api/collections',payload={'title':'Подборка','description':'Маршрут изучения','subject_id':self.sid}).json['id'];url=f'/api/collections/{cid}/items'
        self.assertEqual(self.req(self.a,url,'PUT',{'post_ids':[p,q]}).status_code,200)
        self.assertEqual(self.req(self.b,url,'PUT',{'post_ids':[]}).status_code,403)
        self.req(self.a,url,'PUT',{'post_ids':[q,p]});self.assertEqual(self.a.get(f'/api/collections/{cid}').json['items'][0]['post_id'],q)
        self.req(self.a,url,'PUT',{'post_ids':[p]});self.assertEqual(self.a.get(f'/api/posts/{q}').status_code,200)
        with sqlite3.connect(DB) as c:self.assertIsNotNone(c.execute('SELECT reversed_at FROM xp_events WHERE event_key=?',('collection:'+str(cid),)).fetchone()[0])
        self.req(self.a,f'/api/posts/{p}/delete','DELETE');self.assertIsNone(self.a.get(f'/api/collections/{cid}').json['items'][0]['post'])
    def test_08_search_filters(self):
        p=self.create();r=self.guest.get('/api/posts?q=геометрия&kind=work&subject_id='+str(self.sid)).json
        self.assertIn(p,[x['id'] for x in r['items']]);self.assertEqual(r['total'],len(r['items']))
        self.assertEqual(self.guest.get('/api/posts?q=zzzzzzzz').json['total'],0)
    def test_09_profile_xp_role_separation(self):
        for _ in range(2):self.req(self.a,'/api/profile/update',payload={'display_name':'Алиса','bio':'Исследую','school':'','achievements':''})
        p=self.a.get('/api/profile/alice').json;self.assertEqual(p['role'],'user');self.assertNotIn('password',p)
        self.assertNotIn('events',self.b.get('/api/profile/alice').json)
        with sqlite3.connect(DB) as c:self.assertEqual(c.execute("SELECT count(*) FROM xp_events WHERE event_key='profile:alice'").fetchone()[0],1)
        self.assertEqual(self.a.get('/api/admin').status_code,403)
    def test_10_restart_migration_and_missing(self):
        p=self.create();migrate();self.assertEqual(app.test_client().get(f'/api/posts/{p}').status_code,200)
        self.assertEqual(self.guest.get('/api/posts/999999').status_code,404)
        self.assertEqual(self.guest.get('/api/profile/nobody').status_code,404)
    def test_11_reports_moderation_audit(self):
        p=self.create();self.assertEqual(self.req(self.b,'/api/reports',payload={'post_id':p,'reason':'Проверка'}).status_code,200)
        self.assertEqual(self.b.get('/api/reports').status_code,403)
        with sqlite3.connect(DB) as c:c.execute("UPDATE users SET role='moderator' WHERE username='bob'")
        rid=self.b.get('/api/reports').json['items'][0]['id'];self.assertEqual(self.req(self.b,f'/api/reports/{rid}','PUT',{'resolution':'Проверено','remove':True}).status_code,200)
        self.assertEqual(self.a.get(f'/api/posts/{p}').status_code,404)
        with sqlite3.connect(DB) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM audit').fetchone()[0],1);c.execute("UPDATE users SET role='user' WHERE username='bob'")

    def test_12_limits_validation_password(self):
        for _ in range(5):self.create()
        with sqlite3.connect(DB) as c:
            n=c.execute("SELECT sum(amount) FROM xp_events WHERE username='alice' AND kind='publication'").fetchone()[0]
            self.assertLessEqual(n,60)
        body=dict(title='Тест',content='Текст',kind='invalid',publication_status='published',subject_id=self.sid,topic_id=self.tid)
        self.assertEqual(self.req(self.a,'/api/posts',payload=body).status_code,400)
        body['kind']='work';body['topic_id']=999999
        self.assertEqual(self.req(self.a,'/api/posts',payload=body).status_code,400)
        self.assertEqual(self.req(self.a,'/api/password',payload={'current_password':'wrong','new_password':'New-password-123'}).status_code,403)
        self.assertEqual(self.req(self.a,'/api/password',payload={'current_password':'A-test-password-123','new_password':'New-password-123'}).status_code,200)

    def test_13_deletion_requests_access_and_rejection(self):
        p=self.create();payload={'post_id':p,'kind':'deletion','reason':'Прошу удалить эту публикацию'}
        self.assertEqual(self.req(self.guest,'/api/reports',payload=payload).status_code,401)
        self.assertEqual(self.req(self.b,'/api/reports',payload={**payload,'reason':' '}).status_code,400)
        r=self.req(self.b,'/api/reports',payload=payload);self.assertEqual(r.status_code,200);rid=r.json['id']
        self.assertEqual(self.req(self.b,'/api/reports',payload=payload).status_code,409)
        self.assertIn(rid,[r['id'] for r in self.b.get('/api/my/reports').json['items']])
        self.assertNotIn(rid,[r['id'] for r in self.a.get('/api/my/reports').json['items']])
        self.assertNotIn(rid,[r['id'] for r in self.mod.get('/api/reports').json['items']])
        self.assertIn(rid,[r['id'] for r in self.admin.get('/api/reports').json['items']])
        decision={'remove':False,'resolution':'Оснований для удаления нет'}
        for client in [self.a,self.b,self.mod]:self.assertEqual(self.req(client,f'/api/reports/{rid}','PUT',decision).status_code,403)
        self.assertEqual(self.req(self.admin,f'/api/reports/{rid}','PUT',{**decision,'remove':'false'}).status_code,400)
        self.assertEqual(self.req(self.admin,f'/api/reports/{rid}','PUT',decision).status_code,200)
        self.assertEqual(self.req(self.admin,f'/api/reports/{rid}','PUT',decision).status_code,409)
        self.assertEqual(self.guest.get(f'/api/posts/{p}').status_code,200)
        result=next(r for r in self.b.get('/api/my/reports').json['items'] if r['id']==rid)
        self.assertEqual(result['outcome'],'kept');self.assertEqual(result['resolved_by'],'administrator')

    def test_14_admin_edit_preserves_author_xp_and_other_rights(self):
        p=self.create();before=self.a.get(f'/api/posts/{p}').json['post']
        body={**before,'title':'Исправлено администратором','author':'administrator','content':'Уточнённое условие'}
        self.assertEqual(self.req(self.mod,f'/api/posts/{p}','PUT',body).status_code,403)
        token=str(uuid.uuid4())
        for _ in range(2):self.assertEqual(self.req(self.admin,f'/api/posts/{p}','PUT',body,token=token).status_code,200)
        after=self.a.get(f'/api/posts/{p}').json['post'];self.assertEqual(after['author'],'alice');self.assertEqual(after['title'],body['title'])
        self.assertEqual(self.admin.get('/api/profile/administrator').json['xp'],0)
        with sqlite3.connect(DB) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM audit WHERE action='admin_edit_post' AND entity=?",(str(p),)).fetchone()[0],1)
            self.assertEqual(c.execute('SELECT username FROM xp_events WHERE event_key=?',('post:'+str(p),)).fetchone()[0],'alice')
        draft=self.create('draft')
        self.assertEqual(self.admin.get(f'/api/posts/{draft}').status_code,404)
        self.assertEqual(self.req(self.admin,f'/api/posts/{draft}','PUT',body).status_code,404)
        cid=self.req(self.a,f'/api/posts/{p}/comments',payload={'text':'Мой комментарий'}).json['id']
        self.assertEqual(self.req(self.admin,f'/api/comments/{cid}','PUT',{'text':'Чужое'}).status_code,403)

    def test_15_admin_direct_delete_closes_requests(self):
        p=self.create();payload={'post_id':p,'kind':'deletion','reason':'Удаление'}
        rid=self.req(self.b,'/api/reports',payload=payload).json['id']
        self.assertEqual(self.req(self.mod,f'/api/posts/{p}/delete','DELETE').status_code,403)
        self.assertEqual(self.req(self.admin,f'/api/posts/{p}/delete','DELETE').status_code,200)
        self.assertEqual(self.guest.get(f'/api/posts/{p}').status_code,404)
        result=next(r for r in self.b.get('/api/my/reports').json['items'] if r['id']==rid)
        self.assertEqual(result['outcome'],'deleted');self.assertEqual(result['status'],'resolved')
        with sqlite3.connect(DB) as c:
            self.assertIsNotNone(c.execute('SELECT reversed_at FROM xp_events WHERE event_key=?',('post:'+str(p),)).fetchone()[0])
            self.assertEqual(c.execute("SELECT count(*) FROM audit WHERE action='admin_delete_post' AND entity=?",(str(p),)).fetchone()[0],1)

    def test_16_approved_request_and_migration_preserve_records(self):
        p=self.create();rid=self.req(self.b,'/api/reports',payload={'post_id':p,'kind':'deletion','reason':'Подтверждённое основание'}).json['id']
        before=self.b.get('/api/my/reports').json['items'];migrate();self.assertEqual(before,self.b.get('/api/my/reports').json['items'])
        self.assertEqual(self.req(self.admin,f'/api/reports/{rid}','PUT',{'remove':True,'resolution':'Заявление удовлетворено'}).status_code,200)
        self.assertEqual(self.a.get(f'/api/posts/{p}').status_code,404)
        result=next(r for r in self.b.get('/api/my/reports').json['items'] if r['id']==rid)
        self.assertEqual(result['resolution'],'Заявление удовлетворено');self.assertEqual(result['post_title'],'Геометрия треугольника')
        self.assertEqual(self.req(self.admin,f'/api/reports/{rid}','PUT',{'remove':False,'resolution':'Повторное решение'}).status_code,409)

    def test_17_section_independent_of_content(self):
        p=self.create();body=self.a.get(f'/api/posts/{p}').json['post']
        body['section']='Треугольники';self.assertEqual(self.req(self.a,f'/api/posts/{p}','PUT',body).status_code,200)
        result=self.a.get(f'/api/posts/{p}').json['post']
        self.assertEqual(result['section'],'Треугольники');self.assertEqual(result['content'],'Условие и анализ');self.assertEqual(result['topic_id'],self.tid)
        body['section']='x'*121;self.assertEqual(self.req(self.a,f'/api/posts/{p}','PUT',body).status_code,400)

    def test_18_comment_images_reports_and_admin_deletion(self):
        p=self.create(kind='task');image=io.BytesIO();Image.new('RGB',(12,12),'red').save(image,'PNG');raw=image.getvalue()
        def send(parent=None,token=None):
            body={'comment_image':(io.BytesIO(raw),'image.png')}
            if parent:body['parent_id']=str(parent)
            return self.req(self.b,f'/api/posts/{p}/comments',payload=body,multipart=True,token=token)
        token=str(uuid.uuid4());r=send(token=token);self.assertEqual(r.status_code,200,r.json);cid=r.json['id'];self.assertEqual(send(token=token).json['id'],cid)
        reply=send(cid).json['id'];comments=self.a.get(f'/api/posts/{p}').json['comments'];self.assertEqual(len(comments),2)
        url=comments[0]['image'];self.assertEqual(self.guest.get(url).status_code,200)
        self.assertEqual(self.req(self.b,f'/api/posts/{p}/comments',payload={'comment_image':(io.BytesIO(b'not image'),'fake.png')},multipart=True).status_code,400)
        report={'target_type':'comment','comment_id':cid,'reason':'Проверьте изображение'}
        rid=self.req(self.a,'/api/reports',payload=report).json['id'];self.assertEqual(self.req(self.a,'/api/reports',payload=report).status_code,409)
        self.assertNotIn(rid,[r['id'] for r in self.mod.get('/api/reports').json['items']])
        self.assertEqual(self.req(self.mod,f'/api/reports/{rid}','PUT',{'action':'delete','resolution':'Решение'}).status_code,403)
        self.assertEqual(self.req(self.mod,f'/api/comments/{cid}','DELETE').status_code,403)
        self.req(self.a,f'/api/comments/{cid}/accept')
        self.assertEqual(self.req(self.admin,f'/api/reports/{rid}','PUT',{'action':'delete','resolution':'Изображение удалено'}).status_code,200)
        result=self.a.get(f'/api/posts/{p}').json;self.assertTrue(result['comments'][0]['deleted']);self.assertFalse(result['comments'][1]['deleted']);self.assertFalse(result['comments'][0]['is_accepted']);self.assertEqual(self.guest.get(url).status_code,404)
        rid=self.req(self.a,'/api/reports',payload={**report,'comment_id':reply}).json['id']
        self.assertEqual(self.req(self.admin,f'/api/comments/{reply}','DELETE').status_code,200)
        self.assertEqual(next(r for r in self.a.get('/api/my/reports').json['items'] if r['id']==rid)['outcome'],'deleted')

    def test_19_mute_enforced_expires_and_can_be_appealed(self):
        from datetime import datetime,timedelta,timezone
        p=self.create();cid=self.req(self.b,f'/api/posts/{p}/comments',payload={'text':'До мута'}).json['id']
        endpoint='/api/admin/users/bob/mute'
        self.assertEqual(self.req(self.b,endpoint,'PUT',{'minutes':5}).status_code,403)
        for minutes in (-1,5256001,1.5,True):self.assertEqual(self.req(self.admin,endpoint,'PUT',{'minutes':minutes}).status_code,400)
        self.assertEqual(self.req(self.admin,'/api/admin/users/administrator/mute','PUT',{'minutes':1}).status_code,403)
        rid=self.req(self.a,'/api/reports',payload={'target_type':'user','target_username':'bob','reason':'Нарушение правил'}).json['id']
        self.assertEqual(self.req(self.admin,f'/api/reports/{rid}','PUT',{'action':'mute','minutes':60,'resolution':'Мут на час'}).status_code,200)
        self.assertTrue(self.b.get('/api/session').json['user']['muted'])
        for url,method,body in [(f'/api/posts/{p}/comments','POST',{'text':'Запрещено'}),('/api/posts','POST',{}),(f'/api/comments/{cid}','PUT',{'text':'Изменить'}),('/api/profile/update','POST',{}),('/api/collections','POST',{})]:
            self.assertEqual(self.req(self.b,url,method,body).status_code,403,url)
        self.assertEqual(self.b.get(f'/api/posts/{p}').status_code,200)
        self.assertEqual(self.req(self.b,'/api/reports',payload={'target_type':'user','target_username':'administrator','reason':'Обжалование'}).status_code,200)
        with sqlite3.connect(DB) as c:c.execute('UPDATE users SET muted_until=? WHERE username=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),'bob'))
        self.assertFalse(self.b.get('/api/session').json['user']['muted'])
        self.assertEqual(self.req(self.b,f'/api/posts/{p}/comments',payload={'text':'После истечения'}).status_code,200)
        self.req(self.admin,endpoint,'PUT',{'minutes':30});self.req(self.admin,endpoint,'PUT',{'minutes':0});self.assertFalse(self.b.get('/api/session').json['user']['muted'])

    def test_20_account_age_and_deletion_preserve_materials(self):
        from datetime import datetime,timedelta,timezone
        client=app.test_client();self.req(client,'/api/register',payload={'username':'temporary','password':'A-test-password-123'})
        self.assertEqual(client.get('/api/profile/temporary').json['days_on_site'],0)
        with sqlite3.connect(DB) as c:c.execute('UPDATE users SET created_at=? WHERE username=?',((datetime.now(timezone.utc)-timedelta(days=12,hours=3)).isoformat(),'temporary'))
        self.assertEqual(client.get('/api/profile/temporary').json['days_on_site'],12)
        with sqlite3.connect(DB) as c:c.execute('UPDATE users SET created_at=NULL WHERE username=?',('temporary',))
        self.assertIsNone(client.get('/api/profile/temporary').json['days_on_site'])
        body=dict(title='Сохраняемый материал',content='Содержание',subject_id=self.sid,topic_id=self.tid,kind='work',publication_status='published')
        p=self.req(client,'/api/posts',payload=body).json['id'];cid=self.req(client,f'/api/posts/{p}/comments',payload={'text':'Сохраняемый комментарий'}).json['id']
        body['publication_status']='draft';draft=self.req(client,'/api/posts',payload=body).json['id']
        self.assertEqual(self.req(self.admin,'/api/admin/users/administrator','DELETE').status_code,403)
        self.assertEqual(self.req(self.b,'/api/admin/users/temporary','DELETE').status_code,403)
        self.assertEqual(self.req(self.admin,'/api/admin/users/temporary','DELETE').status_code,200)
        self.assertIsNone(client.get('/api/session').json['user']);self.assertEqual(client.get(f'/api/posts/{draft}').status_code,404)
        self.assertEqual(self.guest.get('/api/profile/temporary').status_code,404)
        result=self.guest.get(f'/api/posts/{p}').json;self.assertTrue(result['post']['author_deleted']);self.assertTrue(result['comments'][0]['author_deleted']);self.assertEqual(result['comments'][0]['id'],cid)
        self.assertEqual(self.req(client,'/api/login',payload={'username':'temporary','password':'A-test-password-123'}).status_code,401)
        self.assertEqual(self.req(client,'/api/register',payload={'username':'temporary','password':'A-test-password-123'}).status_code,409)
        migrate();self.assertTrue(self.guest.get(f'/api/posts/{p}').json['post']['author_deleted'])

    def test_21_registration_unicode_case_and_login(self):
        c=app.test_client();other=app.test_client()
        self.assertEqual(self.req(c,'/api/register',payload={'username':'ТестовыйАвтор','password':'Test-password-123'}).status_code,201)
        self.assertEqual(self.req(other,'/api/register',payload={'username':'тестовыйавтор','password':'Test-password-123'}).status_code,409)
        self.assertEqual(self.req(other,'/api/login',payload={'username':'ТЕСТОВЫЙАВТОР','password':'Test-password-123'}).status_code,200)
        self.assertEqual(other.get('/api/session').json['user']['username'],'ТестовыйАвтор')
        self.assertEqual(self.req(other,'/api/login',payload={'username':'ТЕСТОВЫЙАВТОР','password':'wrong-password'}).status_code,401)

    def test_22_invalid_payload_and_csrf_are_recoverable(self):
        for body in ([1],True,'text',None):
            token=self.a.get('/api/session').json['csrf']
            r=self.a.post('/api/register',data=__import__('json').dumps(body),content_type='application/json',headers={'X-CSRF-Token':token})
            self.assertEqual(r.status_code,400,r.data)
        r=self.a.post('/api/posts',json={},headers={'X-CSRF-Token':'stale'})
        self.assertEqual(r.status_code,403);self.assertEqual(r.json['code'],'csrf_expired')
        p=self.create()
        for value in (True,1.5):
            self.assertEqual(self.req(self.a,'/api/bookmarks/toggle',payload={'post_id':value}).status_code,400)
        body=self.a.get(f'/api/posts/{p}').json['post'];body['section']='НоваяПодтемаДляПоиска'
        self.req(self.a,f'/api/posts/{p}','PUT',body)
        self.assertIn(p,[x['id'] for x in self.guest.get('/api/posts?q=новаяподтемадляпоиска').json['items']])

if __name__=='__main__':unittest.main(verbosity=2)

