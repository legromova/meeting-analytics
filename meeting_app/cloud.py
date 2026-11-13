"""Private object storage and durable HTTP dispatch for the Vercel mode."""
import os
from pathlib import Path
from urllib.parse import quote, urlparse
import httpx

REQUIRED = ('DATABASE_URL','SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY','QSTASH_TOKEN','MEETING_WORKER_SECRET','MEETING_BOOTSTRAP_TOKEN','MEETING_PUBLIC_URL')


def enabled():
    return os.getenv('MEETING_CLOUD') == '1' or os.getenv('VERCEL') == '1'


def missing():
    keys = REQUIRED
    if os.getenv('MEETING_QUEUE_PROVIDER') == 'vercel':
        keys = tuple(key for key in keys if key not in ('QSTASH_TOKEN','MEETING_WORKER_SECRET','MEETING_PUBLIC_URL'))
    absent = [key for key in keys if not os.getenv(key)]
    url = os.getenv('DATABASE_URL', '')
    if url.startswith(('postgresql://', 'postgres://')) and not urlparse(url).password and not os.getenv('MEETING_DATABASE_PASSWORD'):
        absent.append('MEETING_DATABASE_PASSWORD')
    return absent


def storage():
    origin = os.environ['SUPABASE_URL'].rstrip('/')
    if urlparse(origin).scheme != 'https':
        raise ValueError('Storage must use HTTPS')
    return origin+'/storage/v1', os.getenv('SUPABASE_STORAGE_BUCKET','meetings')


def headers():
    key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    return {'apikey':key, 'Authorization':'Bearer '+key}


def object_path(path):
    if path.startswith('/') or '..' in path.split('/'):
        raise ValueError('Unsafe storage path')
    return quote(path, safe='/')


def signed(path, upload=False):
    base, bucket = storage()
    route = 'object/upload/sign/' if upload else 'object/sign/'
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.post(base+'/'+route+bucket+'/'+object_path(path), headers=headers(), json={} if upload else {'expiresIn':300})
        response.raise_for_status()
    data = response.json()
    relative = data.get('signedURL') or data.get('signedUrl') or data.get('url')
    if not relative:
        raise ValueError('No signed storage URL')
    return base+relative if relative.startswith('/') else relative


def put(path, source):
    base, bucket = storage()
    with Path(source).open('rb') as content, httpx.Client(timeout=120, trust_env=False) as client:
        response = client.post(base+'/object/'+bucket+'/'+object_path(path), headers={**headers(),'Content-Type':'application/octet-stream','x-upsert':'true'}, content=content)
        response.raise_for_status()


def download(path, destination, limit=200*1024*1024):
    base, bucket = storage()
    size = 0
    with httpx.Client(timeout=120, trust_env=False) as client:
        with client.stream('GET',base+'/object/authenticated/'+bucket+'/'+object_path(path),headers=headers()) as response:
            response.raise_for_status()
            with Path(destination).open('wb') as out:
                for chunk in response.iter_bytes(1024*1024):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Размер файла превышает 200 МиБ')
                    out.write(chunk)


def delete(path):
    base, bucket = storage()
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.request('DELETE',base+'/object/'+bucket,headers=headers(),json={'prefixes':[path]})
        response.raise_for_status()


def enqueue(mid):
    if os.getenv('MEETING_QUEUE_PROVIDER') == 'vercel':
        from vercel.queue.sync import send
        return send('meeting-processing', {'meeting_id': mid})
    url = os.environ['MEETING_PUBLIC_URL'].rstrip('/')+'/api/cloud/worker'
    if urlparse(url).scheme != 'https':
        raise ValueError('Worker must use HTTPS')
    with httpx.Client(timeout=15, trust_env=False) as client:
        response = client.post('https://qstash.upstash.io/v2/publish/'+url,headers={
            'Authorization':'Bearer '+os.environ['QSTASH_TOKEN'],
            'Upstash-Forward-X-Worker-Token':os.environ['MEETING_WORKER_SECRET'],
            'Upstash-Timeout':'300s','Upstash-Retries':'3','Upstash-Retry-Delay':'600000','Content-Type':'application/json',
        },json={'meeting_id':mid})
        response.raise_for_status()
