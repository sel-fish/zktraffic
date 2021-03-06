# ==================================================================================================
# Copyright 2014 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================


'''
Captures different stats using the same infrastructure. Registers a handler with the sniffer
and queues all packets. A separate thread dequeues each request and delivers it to multiple
stats handler.
'''

from collections import defaultdict
from threading import Condition

import time

from zktraffic.base.deque import Deque

from .timer import Timer

from twitter.common import log
from twitter.common.exceptions import ExceptionalThread


class QueueStatsLoader(ExceptionalThread):

  def __init__(self, max_reqs=400000, max_reps=400000, max_events=400000, timer=None):
    self._accumulators = {}
    self._cv = Condition()
    self._stopped = True
    self._requests = Deque(maxlen=max_reqs)
    self._replies = Deque(maxlen=max_reps)
    self._events = Deque(maxlen=max_events)
    self._request_handlers = set()
    self._reply_handlers = set()
    self._event_handlers = set()
    self._auth_by_client = defaultdict(lambda: intern("noauth"))
    self._timer = timer if timer else Timer()
    super(QueueStatsLoader, self).__init__()
    self.setDaemon(True)

  def wakeup(self):
    with self._cv:
      self._cv.notify()

  @property
  def auth_by_client(self):
    return self._auth_by_client

  def register_accumulator(self, name, accumulator):
    # TODO : Disallow registration after thread start
    self._accumulators[name] = accumulator
    if hasattr(accumulator, 'update_request_stats'):
      self._request_handlers.add(accumulator.update_request_stats)
    if hasattr(accumulator, 'update_reply_stats'):
      self._reply_handlers.add(accumulator.update_reply_stats)
    if hasattr(accumulator, 'update_event_stats'):
      self._event_handlers.add(accumulator.update_event_stats)

  def stop(self):
    with self._cv:
      self._stopped = True
      self._cv.notify()

  def run(self):
    """ compute stats from queued requests """
    log.info("Starting queue stats loader ...")
    self._stopped = False

    self._timer.reset()
    while not self._stopped:
      # update stats for available requests/replies/events

      self._process_queue(self._requests, self._request_handlers)
      self._process_queue(self._replies, self._reply_handlers)
      self._process_queue(self._events, self._event_handlers)

      if self._timer.after(60):
        for accumulator in self._accumulators.values():
          accumulator.accumulate_stats()
        self._timer.reset()

      # no need to wake up immediately to process the new packets
      time.sleep(1)

  def _process_queue(self, queue, handlers):
    while True:
      try:
        item = queue.pop()
      except IndexError:
        break

      try:
        _ = [handler(item) for handler in handlers]
      except Exception as ex:
        name = getattr(item, "name", "")
        path = getattr(item, "path", "")
        ip = getattr(item, "ip", "")
        log.error("Handler call for item %s, %s, %s failed: %s", name, path, ip, ex)

  def handle_request(self, request):
    if request.is_auth:
      self._auth_by_client[request.client] = request.credential
    else:
      request.auth = self._auth_by_client[request.client]

    self.add_to_queue(self._requests, request, "requests")

  def handle_reply(self, reply):
    self.add_to_queue(self._replies, reply, "replies")

  def handle_event(self, event):
    self.add_to_queue(self._events, event, "events")

  def add_to_queue(self, queue, item, label):
    """ queue items send to us by the sniffer """
    count = len(queue)
    if count > queue.maxlength():  # pragma: no cover
      log.warn("Too many %s queued (%d)", label, count)
      return

    queue.appendleft(item)

  def stats(self, name, top):
    return self._accumulators[name].stats(top)
