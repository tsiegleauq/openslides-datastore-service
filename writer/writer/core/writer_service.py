import threading
from collections import defaultdict
from typing import Dict, List

from shared.di import service_as_factory
from shared.util import logger

from .database import Database
from .event_executor import EventExecutor
from .event_translator import EventTranslator
from .messaging import Messaging
from .occ_locker import OccLocker
from .write_request import WriteRequest


@service_as_factory
class WriterService:

    _lock = threading.Lock()

    database: Database
    occ_locker: OccLocker
    event_translator: EventTranslator
    event_executor: EventExecutor
    messaging: Messaging

    def write(self, write_request: WriteRequest) -> None:
        with self._lock:
            self.write_request = write_request
            # Convert request events to db events
            self.db_events = self.event_translator.translate(write_request.events)

            with self.database.get_context():
                self.write_with_database_context()

            stats: Dict[str, int] = defaultdict(int)
            for event in write_request.events:
                log_name = (
                    type(event)
                    .__name__.replace("Request", "")
                    .replace("Event", "")
                    .upper()
                )
                stats[log_name] += 1
            stats_string = ", ".join(f"{cnt} {name}" for name, cnt in stats.items())
            logger.info(f"Events executed ({stats_string})")

            # Only propagate updates to redis after the transaction has finished
            self.messaging.handle_events(self.db_events, self.position)

    def write_with_database_context(self) -> None:
        # Check locked_fields -> Possible LockedError
        self.assert_locked_fields()

        # Insert db events with position data
        self.position = self.database.insert_events(
            self.db_events, self.write_request.information, self.write_request.user_id
        )

        # Store updated models in the Read-DB
        self.event_executor.update(self.db_events, self.position)

    def assert_locked_fields(self) -> None:
        """ May raise a ModelLockedException """
        self.occ_locker.assert_fqid_positions(self.write_request.locked_fqids)
        self.occ_locker.assert_fqfield_positions(self.write_request.locked_fqfields)
        self.occ_locker.assert_collectionfield_positions(
            self.write_request.locked_collectionfields
        )

    def reserve_ids(self, collection: str, amount: int) -> List[int]:
        with self.database.get_context():
            ids = self.database.reserve_next_ids(collection, amount)
            logger.info(f"{len(ids)} ids reserved")
            return ids

    def truncate_db(self) -> None:
        with self.database.get_context():
            self.database.truncate_db()
            logger.info("Database truncated")
