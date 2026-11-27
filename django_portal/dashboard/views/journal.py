"""Журнал действий: кто, когда и что сделал на сайте."""

from datetime import datetime, time, timedelta

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from ..access import permission_required
from ..audit import ACTION_GROUPS, ACTIONS, DANGEROUS_ACTIONS, action_label
from ..models import AuditEvent
from ..periods import requested_period
from ..roles import role_label

PAGE_SIZE = 50


@permission_required("audit.view")
def journal(request):
    events = AuditEvent.objects.all()
    date_from, date_to = requested_period(request)
    local = timezone.get_default_timezone()
    if date_from:
        start = timezone.make_aware(datetime.combine(datetime.fromisoformat(date_from).date(), time.min), local)
        events = events.filter(created_at__gte=start)
    if date_to:
        end = timezone.make_aware(
            datetime.combine(datetime.fromisoformat(date_to).date() + timedelta(days=1), time.min), local,
        )
        events = events.filter(created_at__lt=end)

    selected_user = request.GET.get("user", "").strip()[:150]
    if selected_user:
        events = events.filter(username=selected_user)
    selected_group = request.GET.get("group", "").strip()
    if selected_group in ACTION_GROUPS:
        events = events.filter(action__in=[code for code, (_, group) in ACTIONS.items() if group == selected_group])
    else:
        selected_group = ""
    selected_action = request.GET.get("action", "").strip()
    if selected_action in ACTIONS:
        events = events.filter(action=selected_action)
    else:
        selected_action = ""
    object_type = request.GET.get("object_type", "").strip()[:40]
    object_id = request.GET.get("object_id", "").strip()[:64]
    if object_type and object_id:
        events = events.filter(object_type=object_type, object_id=object_id)
    search = " ".join(request.GET.get("q", "").split())[:100]
    if search:
        events = events.filter(Q(summary__icontains=search) | Q(username__icontains=search) | Q(object_id=search))

    page = Paginator(events, PAGE_SIZE).get_page(request.GET.get("page"))
    for event in page.object_list:
        event.action_label = action_label(event.action)
        event.group = ACTIONS.get(event.action, ("", ""))[1]
        event.role_label = role_label(event.role) if event.role else ""
        event.dangerous = event.action in DANGEROUS_ACTIONS
        event.local_time = timezone.localtime(event.created_at, local)

    query = request.GET.copy()
    query.pop("page", None)
    usernames = (
        AuditEvent.objects.exclude(username="").order_by("username")
        .values_list("username", flat=True).distinct()
    )
    return render(request, "dashboard/journal.html", {
        "page": page,
        "page_query": query.urlencode(),
        "selected_date_from": date_from,
        "selected_date_to": date_to,
        "selected_user": selected_user,
        "selected_group": selected_group,
        "selected_action": selected_action,
        "search_text": search,
        "object_type": object_type,
        "object_id": object_id,
        "usernames": list(usernames),
        "groups": ACTION_GROUPS,
        "actions": [(code, label) for code, (label, _) in ACTIONS.items()],
        "has_filters": any(request.GET.get(key) for key in ("date_from", "date_to", "user", "group", "action", "q", "object_id")),
    })
