"""Пользователи сайта и роли: кто входит в панель и что ему можно."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .. import audit
from ..access import is_creator, permission_required, staff_profile, user_role
from ..models import StaffProfile
from ..roles import (
    ASSIGNABLE_ROLES,
    OWNER,
    PERMISSIONS,
    ROLES,
    permission_matrix,
    role_label,
)


def assignable_roles(user):
    """Какие роли может выдавать текущий пользователь."""
    return ASSIGNABLE_ROLES.get(user_role(user), [])


def can_manage(actor, target):
    """Администратор не трогает владельцев и других администраторов."""
    if actor.pk == target.pk:
        return False
    if is_creator(target):
        return False
    return user_role(target) in assignable_roles(actor)


def clean_regions(raw):
    parts = [" ".join(item.split()) for item in str(raw or "").split(",")]
    return ", ".join(part for part in parts if part)[:1000]


def profile_for(user):
    profile = staff_profile(user)
    if profile is None:
        profile = StaffProfile(user=user, role=user_role(user))
    return profile


@permission_required("users.manage")
def users_list(request):
    user_model = get_user_model()
    rows = []
    for user in user_model.objects.select_related("staff_profile").order_by("-is_active", "username"):
        profile = profile_for(user)
        role = user_role(user)
        rows.append({
            "user": user,
            "full_name": profile.full_name,
            "role": role,
            "role_label": role_label(role),
            "regions": profile.region_list() if ROLES[role]["scoped"] else [],
            "scoped": ROLES[role]["scoped"],
            "is_owner_setting": is_creator(user),
            "can_manage": can_manage(request.user, user),
            "is_self": user.pk == request.user.pk,
        })
    return render(request, "dashboard/users.html", {
        "rows": rows,
        "roles": [
            {"code": code, "label": role["label"], "description": role["description"], "scoped": role["scoped"]}
            for code, role in ROLES.items()
        ],
        "assignable": [(code, role_label(code)) for code in assignable_roles(request.user)],
        "matrix": permission_matrix(),
        "permission_count": len(PERMISSIONS),
        "known_regions": known_regions(),
    })


def known_regions():
    """Города из базы бота — подсказка при вводе регионов."""
    from ..db import bot_database

    connection = bot_database()
    if not connection:
        return []
    try:
        rows = connection.execute(
            "SELECT DISTINCT TRIM(city) AS city FROM users WHERE TRIM(COALESCE(city, '')) != '' ORDER BY city"
        ).fetchall()
    except Exception:  # noqa: BLE001 — подсказка не обязательна
        return []
    finally:
        connection.close()
    return [row["city"] for row in rows]


@permission_required("users.manage")
@require_POST
def user_add(request):
    user_model = get_user_model()
    username = request.POST.get("username", "").strip()
    password = request.POST.get("password", "")
    full_name = " ".join(request.POST.get("full_name", "").split())[:200]
    role = request.POST.get("role", "")
    regions = clean_regions(request.POST.get("regions", ""))
    if role not in assignable_roles(request.user):
        messages.error(request, "Эту роль вы назначать не можете.")
        return redirect("users")
    if len(username) < 3:
        messages.error(request, "Логин должен быть не короче 3 символов.")
        return redirect("users")
    if settings.CREATOR_DJANGO_USERNAME and username == settings.CREATOR_DJANGO_USERNAME:
        messages.error(request, "Этот логин зарезервирован для владельца.")
        return redirect("users")
    if user_model.objects.filter(username__iexact=username).exists():
        messages.error(request, "Такой логин уже есть.")
        return redirect("users")
    try:
        validate_password(password, user_model(username=username))
    except ValidationError as error:
        messages.error(request, "Пароль не подходит: " + " ".join(error.messages))
        return redirect("users")
    with transaction.atomic():
        user = user_model.objects.create_user(
            username=username, password=password, is_staff=False, is_superuser=False,
        )
        StaffProfile.objects.create(user=user, full_name=full_name, role=role, regions=regions)
    audit.record(
        request, "user_add",
        f"Создан вход {username}" + (f" ({full_name})" if full_name else "") + f": {role_label(role)}"
        + (f", регионы: {regions}" if regions and ROLES[role]["scoped"] else ""),
        object_type="user", object_id=user.pk, role=role, regions=regions,
    )
    messages.success(request, f"Вход создан: {username} — {role_label(role)}.")
    return redirect("users")


@permission_required("users.manage")
def user_edit(request, user_id):
    user_model = get_user_model()
    try:
        target = user_model.objects.select_related("staff_profile").get(pk=user_id)
    except user_model.DoesNotExist as missing:
        raise Http404 from missing
    if not can_manage(request.user, target):
        messages.error(request, "Этого пользователя вы изменять не можете.")
        return redirect("users")
    profile = profile_for(target)

    if request.method == "POST":
        role = request.POST.get("role", "")
        if role not in assignable_roles(request.user):
            messages.error(request, "Эту роль вы назначать не можете.")
            return redirect("user_edit", user_id=user_id)
        before = {
            "full_name": profile.full_name, "role": user_role(target),
            "regions": profile.regions, "is_active": target.is_active,
        }
        new_password = request.POST.get("password", "")
        if new_password:
            try:
                validate_password(new_password, target)
            except ValidationError as error:
                messages.error(request, "Пароль не подходит: " + " ".join(error.messages))
                return redirect("user_edit", user_id=user_id)
        with transaction.atomic():
            profile.full_name = " ".join(request.POST.get("full_name", "").split())[:200]
            profile.role = role
            profile.regions = clean_regions(request.POST.get("regions", ""))
            profile.save()
            target.is_active = bool(request.POST.get("is_active"))
            if new_password:
                target.set_password(new_password)
            target.save()
        after = {
            "full_name": profile.full_name, "role": role,
            "regions": profile.regions, "is_active": target.is_active,
        }
        changed = [
            f"{label}: «{before[key] if key != 'role' else role_label(before[key])}» → "
            f"«{after[key] if key != 'role' else role_label(after[key])}»"
            for key, label in (("full_name", "ФИО"), ("role", "роль"), ("regions", "регионы"), ("is_active", "вход разрешён"))
            if before[key] != after[key]
        ]
        if changed:
            audit.record(
                request, "user_edit", f"{target.username}: " + "; ".join(changed),
                object_type="user", object_id=target.pk, before=before, after=after,
            )
        if new_password:
            audit.record(request, "user_password", f"Сменён пароль пользователя {target.username}",
                         object_type="user", object_id=target.pk)
        messages.success(request, f"Изменения для {target.username} сохранены.")
        return redirect("users")

    return render(request, "dashboard/user_form.html", {
        "target": target,
        "profile": profile,
        "role": user_role(target),
        "assignable": [(code, role_label(code)) for code in assignable_roles(request.user)],
        "roles": ROLES,
        "known_regions": known_regions(),
        "owner_code": OWNER,
    })
