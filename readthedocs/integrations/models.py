"""Integration models for external services"""

import json
import uuid
import re

from django.db import models, transaction
from django.utils.translation import ugettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.utils.safestring import mark_safe
from rest_framework import status
from jsonfield import JSONField
from pygments import highlight
from pygments.lexers import JsonLexer
from pygments.formatters import HtmlFormatter

from readthedocs.projects.models import Project
from .utils import normalize_request_payload


class HttpExchangeManager(models.Manager):

    """HTTP exchange manager methods"""

    # Filter rules for request headers to remove from the output
    REQ_FILTER_RULES = [
        re.compile('^X-Forwarded-.*$', re.I),
        re.compile('^X-Real-Ip$', re.I),
    ]

    @transaction.atomic
    def from_exchange(self, req, resp, related_object, payload=None):
        """Create object from Django request and response objects

        If an explicit Request ``payload`` is not specified, the payload will be
        determined directly from the Request object. This makes a good effort to
        normalize the data, however we don't enforce that the payload is JSON

        :param req: Request object to store
        :type req: HttpRequest
        :param resp: Response object to store
        :type resp: HttpResponse
        :param related_object: Object to use for generic relation
        :param payload: Alternate payload object to store
        :type payload: dict
        """
        request_payload = payload
        if request_payload is None:
            request_payload = normalize_request_payload(req)
        try:
            request_body = json.dumps(request_payload, sort_keys=True)
        except TypeError:
            request_body = str(request_payload)
        # This is the rawest form of request header we have, the WSGI
        # headers. HTTP headers are prefixed with `HTTP_`, which we remove,
        # and because the keys are all uppercase, we'll normalize them to
        # title case-y hyphen separated values.
        request_headers = dict(
            (key[5:].title().replace('_', '-'), str(val))
            for (key, val) in req.META.items()
            if key.startswith('HTTP_')
        )
        request_headers['Content-Type'] = req.content_type
        # Remove unwanted headers
        for filter_rule in self.REQ_FILTER_RULES:
            for key in request_headers.keys():
                if filter_rule.match(key):
                    del request_headers[key]

        response_payload = resp.data if hasattr(resp, 'data') else resp.content
        try:
            response_body = json.dumps(response_payload, sort_keys=True)
        except TypeError:
            response_body = str(response_payload)
        response_headers = dict(resp.items())

        fields = {
            'status_code': resp.status_code,
            'request_headers': request_headers,
            'request_body': request_body,
            'response_body': response_body,
            'response_headers': response_headers,
        }
        fields['related_object'] = related_object
        obj = self.create(**fields)
        self.delete_limit(related_object)
        return obj

    def delete_limit(self, related_object, limit=10):
        if isinstance(related_object, Integration):
            queryset = self.filter(integrations=related_object)
        else:
            queryset = self.filter(
                content_type=ContentType.objects.get(
                    app_label=related_object._meta.app_label,
                    model=related_object._meta.model_name,
                ),
                object_id=related_object.pk
            )
        for exchange in queryset[limit:]:
            exchange.delete()


class HttpExchange(models.Model):

    """HTTP request/response exchange"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    related_object = GenericForeignKey('content_type', 'object_id')

    date = models.DateTimeField(_('Date'), auto_now_add=True)

    request_headers = JSONField(_('Request headers'))
    request_body = models.TextField(_('Request body'))

    response_headers = JSONField(_('Request headers'))
    response_body = models.TextField(_('Response body'))

    status_code = models.IntegerField(
        _('Status code'), default=status.HTTP_200_OK
    )

    objects = HttpExchangeManager()

    class Meta:
        ordering = ['-date']

    def __unicode__(self):
        return _('Exchange {0}').format(self.pk)

    @property
    def failed(self):
        # Assume anything that isn't 2xx level status code is an error
        return int(self.status_code / 100) != 2

    def formatted_json(self, field):
        """Try to return pretty printed and Pygment highlighted code"""
        value = getattr(self, field) or ''
        try:
            json_value = json.dumps(json.loads(value), sort_keys=True, indent=2)
            formatter = HtmlFormatter()
            html = highlight(json_value, JsonLexer(), formatter)
            return mark_safe(html)
        except (ValueError, TypeError):
            return value

    @property
    def formatted_request_body(self):
        return self.formatted_json('request_body')

    @property
    def formatted_response_body(self):
        return self.formatted_json('response_body')


class IntegrationQuerySet(models.QuerySet):

    """Return a subclass of Integration, based on the integration type

    .. note::
        This doesn't affect queries currently, only fetching of an object
    """

    def get(self, *args, **kwargs):
        """Replace model instance on Integration subclasses

        This is based on the ``integration_type`` field, and is used to provide
        specific functionality to and integration via a proxy subclass of the
        Integration model.
        """
        old = super(IntegrationQuerySet, self).get(*args, **kwargs)
        # Build a mapping of integration_type -> class dynamically
        class_map = dict(
            (cls.integration_type_id, cls)
            for cls in self.model.__subclasses__()
            if hasattr(cls, 'integration_type_id')
        )
        cls_replace = class_map.get(old.integration_type)
        if cls_replace is None:
            return old
        new = cls_replace()
        for k, v in old.__dict__.items():
            new.__dict__[k] = v
        return new


class Integration(models.Model):

    """Inbound webhook integration for projects"""

    GITHUB_WEBHOOK = 'github_webhook'
    BITBUCKET_WEBHOOK = 'bitbucket_webhook'
    GITLAB_WEBHOOK = 'gitlab_webhook'
    API_WEBHOOK = 'api_webhook'

    WEBHOOK_INTEGRATIONS = (
        (GITHUB_WEBHOOK, _('GitHub incoming webhook')),
        (BITBUCKET_WEBHOOK, _('Bitbucket incoming webhook')),
        (GITLAB_WEBHOOK, _('GitLab incoming webhook')),
        (API_WEBHOOK, _('Generic API incoming webhook')),
    )

    INTEGRATIONS = WEBHOOK_INTEGRATIONS

    project = models.ForeignKey(Project, related_name='integrations')
    integration_type = models.CharField(
        _('Integration type'),
        max_length=32,
        choices=INTEGRATIONS
    )
    provider_data = JSONField(_('Provider data'))
    exchanges = GenericRelation(
        'HttpExchange',
        related_query_name='integrations'
    )

    objects = IntegrationQuerySet.as_manager()

    # Integration attributes
    has_sync = False

    def __unicode__(self):
        return (_('{0} for {1}')
                .format(self.get_integration_type_display(), self.project.name))


class GitHubWebhook(Integration):

    integration_type_id = Integration.GITHUB_WEBHOOK
    has_sync = True

    class Meta:
        proxy = True

    @property
    def can_sync(self):
        try:
            return all((k in self.provider_data) for k in ['id', 'url'])
        except (ValueError, TypeError):
            return False


class BitbucketWebhook(Integration):

    integration_type_id = Integration.BITBUCKET_WEBHOOK
    has_sync = True

    class Meta:
        proxy = True

    @property
    def can_sync(self):
        try:
            return all((k in self.provider_data) for k in ['id', 'url'])
        except (ValueError, TypeError):
            return False
