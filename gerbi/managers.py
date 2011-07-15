# -*- coding: utf-8 -*-
"""Django page CMS ``managers``."""
from gerbi import settings
from gerbi.utils import normalize_url, filter_link
from gerbi.http import get_slug

from django.db import models, connection
from django.db.models import Q
from django.core.cache import cache
from django.contrib.auth.models import User
from django.db.models import Avg, Max, Min, Count

from datetime import datetime


class PageManager(models.Manager):
    """
    Page manager provide several filters to obtain pages :class:`QuerySet`
    that respect the page attributes and project settings.
    """

    def populate_pages(self, parent=None, child=5, depth=5):
        """Create a population of :class:`Page <gerbi.models.Page>`
        for testing purpose."""
        from gerbi.models import Content
        author = User.objects.all()[0]
        if depth == 0:
            return
        p = self.model(parent=parent, author=author,
            status=self.model.PUBLISHED)
        p.save()
        p = self.get(id=p.id)
        Content(body='page-' + str(p.id), type='title',
            language=settings.GERBI_DEFAULT_LANGUAGE, page=p).save()
        Content(body='page-' + str(p.id), type='slug',
            language=settings.GERBI_DEFAULT_LANGUAGE, page=p).save()
        for child in range(1, child + 1):
            self.populate_pages(parent=p, child=child, depth=(depth - 1))

    def on_site(self, site_id=None):
        """Return a :class:`QuerySet` of pages that are published on the site
        defined by the ``SITE_ID`` setting.

        :param site_id: specify the id of the site object to filter with.
        """
        if settings.GERBI_USE_SITE_ID:
            if not site_id:
                site_id = settings.SITE_ID
            return self.filter(sites=site_id)
        return self.all()

    def root(self):
        """Return a :class:`QuerySet` of pages without parent."""
        return self.on_site().filter(parent__isnull=True)

    def navigation(self):
        """Creates a :class:`QuerySet` of the published root pages."""
        return self.on_site().filter(
                status=self.model.PUBLISHED).filter(parent__isnull=True)

    def hidden(self):
        """Creates a :class:`QuerySet` of the hidden pages."""
        return self.on_site().filter(status=self.model.HIDDEN)

    def filter_published(self, queryset):
        """Filter the given pages :class:`QuerySet` to obtain only published
        page."""
        if settings.GERBI_USE_SITE_ID:
            queryset = queryset.filter(sites=settings.SITE_ID)

        queryset = queryset.filter(status=self.model.PUBLISHED)

        if settings.GERBI_SHOW_START_DATE:
            queryset = queryset.filter(publication_date__lte=datetime.now())

        if settings.GERBI_SHOW_END_DATE:
            queryset = queryset.filter(
                Q(publication_end_date__gt=datetime.now()) |
                Q(publication_end_date__isnull=True)
            )

        return queryset

    def published(self):
        """Creates a :class:`QuerySet` of published
        :class:`Page <gerbi.models.Page>`."""
        return self.filter_published(self)

    def drafts(self):
        """Creates a :class:`QuerySet` of drafts using the page's
        :attr:`Page.publication_date`."""
        pub = self.on_site().filter(status=self.model.DRAFT)
        if settings.GERBI_SHOW_START_DATE:
            pub = pub.filter(publication_date__gte=datetime.now())
        return pub

    def expired(self):
        """Creates a :class:`QuerySet` of expired using the page's
        :attr:`Page.publication_end_date`."""
        return self.on_site().filter(
            publication_end_date__lte=datetime.now())

    def from_path(self, complete_path, lang, exclude_drafts=True):
        """Return a :class:`Page <gerbi.models.Page>` according to
        the page's path."""
        if complete_path.endswith("/"):
            complete_path = complete_path[:-1]
        if complete_path.startswith("/"):
            complete_path = complete_path[1:]
        # just return the root page
        if complete_path == '':
            root_pages = self.root()
            if root_pages:
                return root_pages[0]
            else:
                return None

        slug = get_slug(complete_path)
        from gerbi.models import Content
        page_ids = Content.objects.get_page_ids_by_slug(slug)
        pages_list = self.on_site().filter(id__in=page_ids)
        if exclude_drafts:
            pages_list = pages_list.exclude(status=self.model.DRAFT)
        if len(pages_list) == 1:
            if(settings.GERBI_USE_STRICT_URL and
                pages_list[0].get_complete_slug(lang) != complete_path):
                    return None
            return pages_list[0]
        # if more than one page is matching the slug,
        # we need to use the full strict URL
        if len(pages_list) > 1:
            for page in pages_list:
                if page.get_complete_slug(lang) == complete_path:
                    return page
        return None


class ContentManager(models.Manager):
    """:class:`Content <gerbi.models.Content>` manager methods"""

    GERBI_CONTENT_DICT_KEY = "page_content_dict_%d_%s_%d"

    def sanitize(self, content):
        """Sanitize a string in order to avoid possible XSS using
        ``html5lib``."""
        import html5lib
        from html5lib import sanitizer
        p = html5lib.HTMLParser(tokenizer=sanitizer.HTMLSanitizer)
        dom_tree = p.parseFragment(content)
        return dom_tree.toxml()

    def set_or_create_content(self, page, language, ctype, body):
        """Set or create a :class:`Content <gerbi.models.Content>` for a
        particular page and language.

        :param page: the concerned page object.
        :param language: the wanted language.
        :param ctype: the content type.
        :param body: the content of the Content object.
        """
        if settings.GERBI_SANITIZE_USER_INPUT:
            body = self.sanitize(body)
        try:
            content = self.filter(page=page, language=language,
                                  type=ctype).latest('creation_date')
            content.body = body
        except self.model.DoesNotExist:
            content = self.model(page=page, language=language, body=body,
                                 type=ctype)
        content.save()
        return content

    def create_content_if_changed(self, page, language, ctype, body):
        """Create a :class:`Content <gerbi.models.Content>` for a particular
        page and language only if the content has changed from the last
        time.

        :param page: the concerned page object.
        :param language: the wanted language.
        :param ctype: the content type.
        :param body: the content of the Content object.
        """
        if settings.GERBI_SANITIZE_USER_INPUT:
            body = self.sanitize(body)
        try:
            content = self.filter(page=page, language=language,
                                  type=ctype).latest('creation_date')
            if content.body == body:
                return content
        except self.model.DoesNotExist:
            pass
        content = self.create(page=page, language=language, body=body,
                type=ctype)

        # Delete old revisions
        if settings.GERBI_CONTENT_REVISION_DEPTH:
            oldest_content = self.filter(page=page, language=language,
                type=ctype).order_by('-creation_date'
                )[settings.GERBI_CONTENT_REVISION_DEPTH:]
            for c in oldest_content:
                c.delete()

        return content

    def get_content_object(self, page, language, ctype):
        """Gets the latest published :class:`Content <gerbi.models.Content>`
        for a particular page, language and placeholder type."""
        params = {
            'language': language,
            'type': ctype,
            'page': page
        }
        if page.freeze_date:
            params['creation_date__lte'] = page.freeze_date
        return  self.filter(**params).latest()

    def get_content(self, page, language, ctype, language_fallback=False):
        """Gets the latest content string for a particular page, language and
        placeholder.

        :param page: the concerned page object.
        :param language: the wanted language.
        :param ctype: the content type.
        :param language_fallback: fallback to another language if ``True``.
        """
        if not language:
            language = settings.GERBI_DEFAULT_LANGUAGE

        frozen = int(bool(page.freeze_date))
        key = self.GERBI_CONTENT_DICT_KEY % (page.id, ctype, frozen)

        if page._content_dict is None:
            page._content_dict = dict()
        if page._content_dict.get(key, None):
            content_dict = page._content_dict.get(key)
        else:
            content_dict = cache.get(key)

        # fill a dict object for each language, that will create
        # P * L queries.
        # L == number of language, P == number of placeholder in the page.
        # Once generated the result is cached.
        if not content_dict:
            content_dict = {}
            for lang in settings.GERBI_LANGUAGES:
                try:
                    content = self.get_content_object(page, lang[0], ctype)
                    content_dict[lang[0]] = content.body
                except self.model.DoesNotExist:
                    content_dict[lang[0]] = ''
            page._content_dict[key] = content_dict
            cache.set(key, content_dict)

        if language in content_dict and content_dict[language]:
            return filter_link(content_dict[language], page, language, ctype)

        if language_fallback:
            for lang in settings.GERBI_LANGUAGES:
                if lang[0] in content_dict and content_dict[lang[0]]:
                    return filter_link(content_dict[lang[0]], page, lang[0],
                        ctype)
        return ''

    def get_content_slug_by_slug(self, slug):
        """Returns the latest :class:`Content <gerbi.models.Content>`
        slug object that match the given slug for the current site domain.

        :param slug: the wanted slug.
        """
        content = self.filter(type='slug', body=slug)
        if settings.GERBI_USE_SITE_ID:
            content = content.filter(page__sites__id=settings.SITE_ID)
        try:
            content = content.latest('creation_date')
        except self.model.DoesNotExist:
            return None
        else:
            return content

    def get_page_ids_by_slug(self, slug):
        """Return all page's id matching the given slug.
        This function also returns pages that have an old slug
        that match.

        :param slug: the wanted slug.
        """
        ids = self.filter(type='slug', body=slug).values('page_id').annotate(
            max_creation_date=Max('creation_date')
        )
        return [content['page_id'] for content in ids]


class PageAliasManager(models.Manager):
    """:class:`PageAlias <gerbi.models.PageAlias>` manager."""

    def from_path(self, request, path, lang):
        """
        Resolve a request to an alias. returns a
        :class:`PageAlias <gerbi.models.PageAlias>` if the url matches
        no page at all. The aliasing system supports plain
        aliases (``/foo/bar``) as well as aliases containing GET parameters
        (like ``index.php?page=foo``).

        :param request: the request object
        :param path: the complete path to the page
        :param lang: not used
        """
        from gerbi.models import PageAlias

        url = normalize_url(path)
        # §1: try with complete query string
        query = request.META.get('QUERY_STRING')
        if query:
            url = url + '?' + query
        try:
            alias = PageAlias.objects.get(url=url)
            return alias
        except PageAlias.DoesNotExist:
            pass
        # §2: try with path only
        url = normalize_url(path)
        try:
            alias = PageAlias.objects.get(url=url)
            return alias
        except PageAlias.DoesNotExist:
            pass
        # §3: not alias found, we give up
        return None