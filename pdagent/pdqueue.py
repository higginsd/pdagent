import errno
import os
import logging
import time
from constants import EVENT_CONSUMED, EVENT_NOT_CONSUMED, EVENT_BAD_ENTRY, \
    EVENT_STOP_ALL, EVENT_BACKOFF_SVCKEY_BAD_ENTRY, \
    EVENT_BACKOFF_SVCKEY_NOT_CONSUMED
from pdagent.jsonstore import JsonStore


logger = logging.getLogger(__name__)

class EmptyQueue(Exception):
    pass


class PDQueue(object):
    """
    A directory based queue for PagerDuty events.

    Notes:
    - Designed for multiple processes concurrently using the queue.
    - Each entry in the queue is written to a separate file in the
        queue directory.
    - Files are named so that sorting by file name is queue order.
    - Concurrent enqueues use exclusive file create & retries to avoid
        using the same file name.
    - Concurrent dequeues are serialized with an exclusive dequeue lock.
    - A dequeue will hold the exclusive lock until the consume callback
        is done.
    - dequeue never block enqueue, and enqueue never blocks dequeue.
    """

    def __init__(self, queue_config, lock_class):
        self.queue_dir = queue_config['outqueue_dir']
        self.db_dir = queue_config['db_dir']
        self.lock_class = lock_class

        self._verify_permissions()

        self._dequeue_lockfile = os.path.join(
            self.queue_dir, "dequeue.lock"
            )

        # error-handling: back-off related stuff.
        self.backoff_db = JsonStore("backoff", self.db_dir)
        self.backoff_initial_delay_sec = \
            queue_config['backoff_initial_delay_sec']
        self.backoff_factor = queue_config['backoff_factor']
        self.backoff_max_attempts = queue_config['backoff_max_attempts']

    def _verify_permissions(self):
        def verify(dir):
            if not (os.access(dir, os.R_OK) and os.access(dir, os.W_OK)):
                raise Exception(
                    "Can't read/write to directory %s, please check permissions"
                    % dir
                    )
        verify(self.queue_dir)
        verify(self.db_dir)

    # Get the list of queued files from the queue directory in enqueue order
    def _queued_files(self, file_prefix="pdq_"):
        fnames = [
            f for f in os.listdir(self.queue_dir) if f.startswith(file_prefix)
            ]
        fnames.sort()
        return fnames

    def _abspath(self, fname):
        return os.path.join(self.queue_dir, fname)

    def enqueue(self, service_key, s):
        # write to an exclusive temp file
        _, tmp_fname_abs, tmp_fd = self._open_creat_excl_with_retry(
            "tmp_%%d_%s.txt" % service_key
            )
        os.write(tmp_fd, s)
        # get an exclusive queue entry file
        pdq_fname, pdq_fname_abs, pdq_fd = self._open_creat_excl_with_retry(
            "pdq_%%d_%s.txt" % service_key
            )
        # since we're exclusive on both files, we can safely rename
        # the tmp file
        os.fsync(tmp_fd)  # this seems to be the most we can do for durability
        os.close(tmp_fd)
        # would love to fsync the rename but we're not writing a DB :)
        os.rename(tmp_fname_abs, pdq_fname_abs)
        os.close(pdq_fd)

        return pdq_fname

    def _open_creat_excl_with_retry(self, fname_fmt):
        n = 0
        while True:
            t_millisecs = int(time.time() * 1000)
            fname = fname_fmt % t_millisecs
            fname_abs = self._abspath(fname)
            fd = _open_creat_excl(fname_abs)
            if fd is None:
                n += 1
                if n < 100:
                    time.sleep(0.001)
                    continue
                else:
                    raise Exception(
                        "Too many retries! (Last attempted name: %s)"
                        % fname_abs
                        )
            else:
                return fname, fname_abs, fd

    def dequeue(self, consume_func):
        # process only first event in queue.
        self._process_queue(lambda events: events[0:1], consume_func)

    def flush(self, consume_func):
        # process all events in queue.
        self._process_queue(lambda events: events, consume_func)

    def _process_queue(self, filter_events_to_process_func, consume_func):
        lock = self.lock_class(self._dequeue_lockfile)
        lock.acquire()

        try:
            backoff_data = None
            try:
                backoff_data = self.backoff_db.get()
            except:
                logger.warning(
                    "Unable to load queue-error back-off history",
                    exc_info=True)
            if not backoff_data:
                # first time, or errors during db read...
                backoff_data = {
                    'attempts': {},
                    'next_retries': {}
                }
            svc_key_attempt = backoff_data['attempts']
            svc_key_next_retry = backoff_data['next_retries']

            file_names = self._queued_files()

            if not len(file_names):
                raise EmptyQueue
            file_names = filter_events_to_process_func(file_names)
            err_svc_keys = set()

            def handle_backoff():
                # don't process more events with same service key.
                err_svc_keys.add(svc_key)
                # has back-off threshold been reached?
                cur_attempt = svc_key_attempt.get(svc_key, 0) + 1
                if cur_attempt >= self.backoff_max_attempts:
                    if consume_code == EVENT_BACKOFF_SVCKEY_NOT_CONSUMED:
                        # consume function does not want us to do
                        # anything with the event.
                        # WARNING: We'll still consider this service
                        # key to be erroneous, though, and continue
                        # backing off events in the key. This will
                        # result in a high back-off interval after
                        # enough number of attempts.
                        pass
                    elif consume_code == EVENT_BACKOFF_SVCKEY_BAD_ENTRY:
                        handle_bad_entry()
                        # now that we have handled the bad entry, we'll want
                        # to give the other events in this service key a chance,
                        # so don't consider svc key as erroneous.
                        err_svc_keys.remove(svc_key)
                    else:
                        raise ValueError(
                            "Invalid back-off threshold breach code %d" %
                            consume_code)
                if svc_key in err_svc_keys:
                    svc_key_next_retry[svc_key] = int(time.time()) + \
                        self.backoff_initial_delay_sec * \
                        self.backoff_factor ** (cur_attempt - 1)
                    svc_key_attempt[svc_key] = cur_attempt

            def handle_bad_entry():
                errname = fname.replace("pdq_", "err_")
                errname_abs = self._abspath(errname)
                logger.info(
                    "Bad entry: Renaming %s to %s..." %
                    (fname, errname))
                os.rename(fname_abs, errname_abs)

            for fname in file_names:
                fname_abs = self._abspath(fname)
                # TODO: handle missing file or other errors
                f = open(fname_abs)
                try:
                    s = f.read()
                finally:
                    f.close()

                _, _, svc_key = _get_event_metadata(fname)
                if svc_key not in err_svc_keys and \
                        svc_key_next_retry.get(svc_key, 0) < time.time():
                    consume_code = consume_func(s)

                    if consume_code == EVENT_CONSUMED:
                        # TODO a failure here will mean duplicate event sends
                        os.remove(fname_abs)
                    elif consume_code == EVENT_NOT_CONSUMED:
                        pass
                    elif consume_code == EVENT_STOP_ALL:
                        # don't process any more events.
                        break
                    elif consume_code == EVENT_BAD_ENTRY:
                        handle_bad_entry()
                    elif consume_code == EVENT_BACKOFF_SVCKEY_BAD_ENTRY or \
                            consume_code == EVENT_BACKOFF_SVCKEY_NOT_CONSUMED:
                        handle_backoff()
                    else:
                        raise ValueError(
                            "Unsupported dequeue consume code %d" %
                            consume_code)

            try:
                # persist back-off info.
                self.backoff_db.set(backoff_data)
            except:
                logger.warning(
                    "Unable to save queue-error back-off history",
                    exc_info=True)
        finally:
            lock.release()

    def cleanup(self, delete_before_sec):
        delete_before_time = (int(time.time()) - delete_before_sec) * 1000

        def _cleanup_files(fname_prefix):
            fnames = self._queued_files(fname_prefix)
            for fname in fnames:
                try:
                    _, enqueue_time, _ = _get_event_metadata(fname)
                except:
                    # invalid file-name; we'll not include it in cleanup.
                    logger.info(
                        "Cleanup: ignoring invalid file name %s" % fname)
                    fnames.remove(fname)
                else:
                    if enqueue_time >= delete_before_time:
                        fnames.remove(fname)
            for fname in fnames:
                try:
                    os.remove(self._abspath(fname))
                except IOError as e:
                    logger.warning(
                        "Could not clean up file %s: %s" % (fname, str(e)))

        # clean up bad / temp files created before delete-before-time.
        _cleanup_files("err_")
        _cleanup_files("tmp_")


def _open_creat_excl(fname_abs):
    try:
        return os.open(fname_abs, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except OSError, e:
        if e.errno == errno.EEXIST:
            return None
        else:
            raise

def _get_event_metadata(fname):
    type, enqueue_time_str, service_key = fname.split('.')[0].split('_')
    return type, int(enqueue_time_str), service_key
