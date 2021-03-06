# -*- coding: utf-8 -*-
from RISJbot.spiders.base.huffpospider import HuffPoSpider

# NOTE: Inherits parsing code etc., overriding only the name and start URL.
class HuffPoUKSpider(HuffPoSpider):
    name = 'huffpouk'
    # allowed_domains = ['huffingtonpost.co.uk']
    # A list of XML sitemap files, or suitable robots.txt files with pointers.
    sitemap_urls = ['http://www.huffingtonpost.co.uk/original-content-map.xml'] 

    def parse_page(self, response):
        """@url http://www.huffingtonpost.co.uk/entry/child-car-seat-law-booster-seat-ban_uk_58b42602e4b060480e09c87d
        @returns items 1
        @scrapes bodytext bylines fetchtime firstpubtime modtime headline
        @scrapes keywords section source summary url
        """
        return super().parse_page(response)

