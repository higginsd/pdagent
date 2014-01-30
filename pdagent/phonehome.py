
import json
import logging
import urllib2

from pdagent.constants import AGENT_VERSION, PHONE_HOME_URI
from pdagent.pdthread import RepeatingThread
from pdagent.thirdparty import httpswithverify


logger = logging.getLogger(__name__)


class PhoneHomeThread(RepeatingThread):

    def __init__(
            self,
            heartbeat_frequency_sec,
            pd_queue,
            guid,
            system_info
            ):
        RepeatingThread.__init__(self, heartbeat_frequency_sec)
        self.pd_queue = pd_queue
        self.guid = guid
        self.system_info = system_info

    def tick(self):
        # phone home, sending out system info the first time.
        logger.debug("Phoning home")
        try:
            # TODO finalize keys.
            phone_home_data = {
                "agent_id": self.guid,
                "agent_version": AGENT_VERSION,
                "agent_stats": self.pd_queue.get_status(
                    throttle_info=True, aggregated=True
                    ),
            }
            if self.system_stats:
                phone_home_data['system_info'] = self.system_info
                # system info not sent out after first time
                self.system_info = None

            request = urllib2.Request(PHONE_HOME_URI)
            request.add_header("Content-type", "application/json")
            request.add_data(json.dumps(phone_home_data))
            try:
                response = httpswithverify.urlopen(request)
                result_str = response.read()
            except:
                logger.error("Error while phoning home:", exc_info=True)
                result_str = None

            if result_str:
                try:
                    result = json.loads(result_str)
                except:
                    logger.warning(
                        "Error reading phone-home response data:",
                        exc_info=True)
                    result = {}

                # TODO store heartbeat frequency.
                result.get("heartbeat_frequency_sec")

        except:
            logger.error("Error while phoning home:", exc_info=True)