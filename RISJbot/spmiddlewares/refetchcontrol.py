# -*- coding: utf-8 -*-

# Define here the models for your spider middleware
#
# See documentation in:
# http://doc.scrapy.org/en/latest/topics/spider-middleware.html

#from scrapy_deltafetch.middleware import DeltaFetch

import logging
import os
import pickle
import datetime
import sqlite3

#from twisted.enterprise import adbapi
#from twisted.internet.defer import inlineCallbacks, returnValue
from scrapy.http import Request
from scrapy.item import BaseItem
from scrapy.utils.request import request_fingerprint
from scrapy.utils.project import data_path
from scrapy.utils.python import to_bytes
from scrapy.exceptions import NotConfigured, DontCloseSpider
from scrapy import signals


logger = logging.getLogger(__name__)

class RefetchControl(object):
    """
    This is a spider middleware to ignore requests to pages containing items
    seen in previous crawls of the same spider. It is a modified version of
    scrapy-deltafetch/DeltaFetch v1.2.1.

    RefetchControl differs from the parent DeltaFetch by offering more general
    control over repeated fetching:
     * The option of fetching (limited numbers of) copies of an item, 
       at intervals of not less than a given time. This allows some sane change
       detection.
     * A mechanism for ensuring complete fetches, by trawling RefetchControl's
       database for insufficiently-fetched pages and scheduling them.

    This depends on sqlite3 instead of bsddb3. It should really use
    twisted.enterprise.adbapi, but the process_spider_output() interface only
    takes concrete values rather than Deferred()s, so it's not very useful.
    """

    def __init__(self, crawler):
        self.crawler = crawler
        s = crawler.settings
        self.dir = s.get('REFETCHCONTROL_DIR', os.getcwd())
        self.maxfetches = s.getint('REFETCHCONTROL_MAXFETCHES', 1)
        self.refetchsecs = s.getint('REFETCHCONTROL_REFETCHSECS', 0)
        # If it's not been fetched in this time, we don't want it - probably
        # repeated fetch failures.
        self.agelimit = s.getint('REFETCHCONTROL_AGELIMITSECS',
                                 self.refetchsecs * self.maxfetches)
        self.refetchfromdb = s.getbool('REFETCHCONTROL_REFETCHFROMDB', False)
        # Keep the DB to a reasonable size by removing stale entries
        self.trimdb = s.getbool('REFETCHCONTROL_TRIMDB', False)
        if self.trimdb:
            self.keysrqd = set()
        self.reset = s.getbool('REFETCHCONTROL_RESET', False)
        # Grotty: see _schedule_url()
        self.rqcallback = s.get('REFETCHCONTROL_RQCALLBACK', 'spider.parse')
        self.dbs = {}
        self.stats = crawler.stats
        self.idletrawled = False
        logger.debug("RefetchControl starting; dir: {}, "
                     "maxfetches: {}, refetchsecs: {}, agelimitsect: {}, "
                     "trimdb: {}, reset: {}"
                     "".format(self.dir,
                               self.maxfetches,
                               self.refetchsecs,
                               self.agelimit,
                               self.trimdb,
                               self.reset,
                              )
                    )

    @classmethod
    def from_crawler(cls, crawler):
        if not crawler.settings.getbool('REFETCHCONTROL_ENABLED'):
            raise NotConfigured
        o = cls(crawler)
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(o.spider_idle,   signal=signals.spider_idle)
        return o

    def spider_opened(self, spider):
        if not os.path.exists(self.dir):
            os.makedirs(self.dir)
        dbpath = os.path.join(self.dir,
                              'RefetchControl-%s.sqlite' % spider.name)

        new = False
        if not os.path.isfile(dbpath):
            logger.info("Can't find database file {}. Regenerating.".format(
                            dbpath))
            # Will need regenerating
            new = True

        detect_types = sqlite3.PARSE_DECLTYPES # |sqlite3.PARSE_COLNAMES
        self.dbs[spider.name] = sqlite3.connect(dbpath,
                                                detect_types=detect_types)
        self.dbs[spider.name].isolation_level = None

        if new or self.reset or getattr(spider, 'refetchcontrol_reset', False):
            logger.info("Resetting RefetchControl database.")
            c = self.dbs[spider.name].cursor()
            c.execute("DROP TABLE IF EXISTS records")
            c.execute("DROP INDEX IF EXISTS idx_fetches_time")
            c.execute("CREATE TABLE records (key bytes, url str, "
                        "fetches int, time timestamp, PRIMARY KEY(key)) "
                        "WITHOUT ROWID")
            c.execute("CREATE INDEX idx_fetches_time ON records (fetches, time)")
            self.dbs[spider.name].commit()
        
    def spider_closed(self, spider):
        logger.debug("Closing databases")
        for db in self.dbs.values():
            # Paranoia.
            db.commit()
            db.close()
        logger.debug("Databases closed")

    def spider_idle(self, spider):
        """If an item is fetched once, but then disappears from the feed
           (pushed off the RSS list by new items, for example) it is not
           automatically refetched. For data completeness, this is an issue.
        
           Iterate the database's stored keys, check if any items are
           eligible. If so, queue Requests for them.

           There should be no race with the main process_spider_output
           throughput."""

        if self.idletrawled or not self.refetchfromdb:
            return

        logger.info("Trawling database for unfetched pages.")
#        if self.trimdb:
#            logger.debug("Keys fetched: {}".format(self.keysrqd))

        keystodelete = set()

        c = self.dbs[spider.name].cursor()
        # If it's newer than this, we don't want it
        cutofft = (datetime.datetime.utcnow()
                        - datetime.timedelta(seconds=self.refetchsecs))
        cutoffold = (datetime.datetime.utcnow()
                        - datetime.timedelta(seconds=self.agelimit)) 
#        for row in c.execute('SELECT * FROM records WHERE '
#                                'time <= ? AND time > ? AND fetches < ?',
#                             (cutofft, cutoffold, self.maxfetches)):
        for row in c.execute('SELECT * FROM records'):
            key, url, nf, t = row
#            logger.debug("key: {}, url: {}, nf: {}, t: {}, (cutofft: {}, "
#                         "cutoffold: {}, nf: {})".format(key, url, nf, t,
#                                                         cutofft, cutoffold,
#                                                         nf))
            if t <= cutofft and t > cutoffold and nf < self.maxfetches:
                # This is eligible
                tdiff = datetime.datetime.utcnow() - t
                logger.debug("Scheduling refetch from database crawl "
                             "({} fetches, last at {}, {:.0f} seconds ago, "
                             "min/max secs {}/{}): {}".format(
                                     nf,
                                     t.isoformat(),
                                     tdiff.total_seconds(),
                                     self.refetchsecs,
                                     self.agelimit,
                                     url,
                                 )
                            )
                self._schedule_url(url,
                                   {'refetchcontrol_trawled': True,
                                    'refetchcontrol_key': key,
                                    'refetchcontrol_previous': nf,},
                                   spider)
                self.stats.inc_value('refetchcontrol/trawled', spider=spider)
            elif t <= cutoffold and self.trimdb and key not in self.keysrqd:
                # Not fetched, too old to fetch; delete
                keystodelete.update([key])
        if self.trimdb:
            with self.dbs[spider.name]:
                for k in keystodelete:
                    logger.debug("Deleting: {}".format(k))
                    query = 'DELETE FROM records WHERE key = ?'
                    self.dbs[spider.name].execute(query, (k,))
                    self.stats.inc_value('refetchcontrol/dbkeystrimmed',
                                         spider=spider)
            # The database is shortened, and we want to minimize its size
            # because DotscrapyPersistence is used
            self.dbs[spider.name].execute('VACUUM')
        self.idletrawled = True
        logger.debug("Trawl finished.")


    def _schedule_url(self, url, meta, spider):
        # This is slightly problematic (but unavaoidable).
        # engine.crawl() is not a published interface, and is nocannot VACUUM from within a transactiont
        # to be considered stable per the devs, though there is a
        # good deal of published code that uses it to schedule URLs
        # like this in the absence of an official alternative.
        #
        # Note that the Request is not sent through spider middleware on the
        # way out, as it doesn't come from a spider. It will go through any
        # downloader middleware, and Responses will come through spider
        # middleware as normal.
        #
        # In particular, that means that it won't trigger our own
        # _process_request code.
        #
        # FIXME: Furthermore, the callback is a problem: for spiders which
        #        inherit from XMLFeedSpider, the default spider.parse()
        #        parses the feed, not pages which are fetched from the
        #        feed. Consequently, the RISJ crawlers have implemented
        #        parse_page() as a standard interface for handling web page
        #        Responses, but this does not exist in all spiders. This
        #        makes this code fairly non-portable between spiders with
        #        different interfaces commingled in the same project :-(
        rq = Request(url,
                     callback=eval(self.rqcallback),
                     meta=meta
                    )
        self.crawler.engine.crawl(rq, spider)

    def _process_request(self, r, spider):
        # Is Request; check if a fetch is allowed.
        key = self._get_key(r)

        if self.trimdb:
            self.keysrqd.update([key])

        if 'refetchcontrol_pass' in r.meta:
            logger.debug('Passing: {}'.format(r))
            self.stats.inc_value('refetchcontrol/passed', spider=spider)
            return r

        # Pass this through, so we can link any Response to this request
        r.meta['refetchcontrol_key'] = key

        c = self.dbs[spider.name].cursor().execute(    
                'SELECT url, fetches, time FROM records WHERE key=?', (key,))
        l = c.fetchone()

        if l is None:
            # First fetch. Log and return.
            logger.debug("First fetch: {}".format(r))
            if self.stats:
                self.stats.inc_value('refetchcontrol/firstfetch',
                                     spider=spider)
            r.meta['refetchcontrol_previous'] = 0
            return r

        # Fetched at least once.
        # Are we allowed another fetch? If so, have we waited the
        # minimum allowable period?
        _, nf, t = l
        tdiff = datetime.datetime.utcnow() - t
        if (nf >= self.maxfetches or
               tdiff.total_seconds() < self.refetchsecs or
               tdiff.total_seconds() > self.agelimit):
            # No. Drop.
            if nf < self.maxfetches:
                logger.debug("Not fetching ({}/{} "
                             "fetches, last at {}, {:.0f} seconds "
                             "ago, min secs {}, max secs {}): {}".format(
                                     nf,
                                     self.maxfetches,
                                     t.isoformat(),
                                     tdiff.total_seconds(),
                                     self.refetchsecs,
                                     self.agelimit,
                                     r,
                                 )
                            )
            self.stats.inc_value('refetchcontrol/skipped', spider=spider)
            return None

        # Yes. Log, add to stats, return
        logger.debug("Refetching ({} fetches, "
                     "last at {}, {:.0f} seconds ago, "
                     "min secs {}) {}".format(
                             nf,
                             t.isoformat(),
                             tdiff.total_seconds(),
                             self.refetchsecs,
                             r,
                         )
                    )
        r.meta['refetchcontrol_previous'] = nf
        self.stats.inc_value('refetchcontrol/refetched', spider=spider)
        return r

    def _process_item(self, item, response, spider):
        # Is Item; update the database with the new number of fetches
        # and timestamp, then pass the Item on.

        if response.meta.get('refetchcontrol_pass'):
            sellf.stats.inc_value('refetchcontrol/passed_item', spider=spider)
            # Not to be logged.
            return item

        c = self.dbs[spider.name].cursor()

        query = 'SELECT fetches FROM records WHERE key=?'
        try:
            key = response.meta['refetchcontrol_key']
        except KeyError:
            logger.warning("No meta['refetchcontrol_key'] for {}: {}".format(
                                response, response.meta)
                          )
            key = self._get_key(response.request)
        c.execute(query, (key,))
        l = c.fetchone()

        if l is None:
            nf = 1
        else:
            nf = l[0] + 1

        query = ("INSERT OR REPLACE INTO records(key, url, fetches, time) "
                 "VALUES(?, ?, ?, ?)")
        c.execute(query, (key, response.url, nf, datetime.datetime.utcnow()))
        self.dbs[spider.name].commit()

        # TODO: Consider adding extra middleware to drop if it hasn't
        #       changed since the last fetch?
        if self.stats:
            self.stats.inc_value('refetchcontrol/stored', spider=spider)
        return item
 
    def process_spider_output(self, response, result, spider):
        def _filter(r):
            if isinstance(r, Request):
                return self._process_request(r, spider)
            elif isinstance(r, (BaseItem, dict)):
                return self._process_item(r, response, spider)
            else:
                raise Exception("Object not Request or Item")

        return (r for r in result or () if _filter(r))

    @staticmethod
    def _get_key(request):
        key = (request.meta.get('refetchcontrol_key') or
               request.meta.get('deltafetch_key') or
               request_fingerprint(request)
              )
        # request_fingerprint() returns string `hashlib.sha1().hexdigest()`
        return to_bytes(key)

