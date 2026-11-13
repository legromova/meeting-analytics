"""Private Vercel Queues subscriber. Only a meeting identifier is delivered."""
from vercel.queue import Message, RetryAfter, subscribe
from starlette.concurrency import run_in_threadpool
from meeting_app import db, incoming


@subscribe(topic='meeting-processing', retry_after=600, max_concurrency=1, max_attempts=4)
async def process_meeting(message: Message[dict[str, int]]) -> None:
    mid = message.payload.get('meeting_id')
    if not isinstance(mid, int) or isinstance(mid, bool):
        return
    await run_in_threadpool(incoming.process_one, mid)
    with db.connect() as conn:
        job = conn.execute('SELECT state FROM jobs WHERE meeting_id=?', (mid,)).fetchone()
    if job and job[0] in ('uploaded', 'processing'):
        raise RetryAfter(600)
