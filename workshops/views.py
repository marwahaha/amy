import csv
import datetime
import io
import re

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.forms import SetPasswordForm, PasswordChangeForm
from django.contrib.auth.mixins import (
    PermissionRequiredMixin,
    UserPassesTestMixin,
)
from django.contrib.auth.views import logout_then_login
from django.core.exceptions import (
    ObjectDoesNotExist,
    PermissionDenied,
)
from django.db import IntegrityError, transaction
from django.db.models import (
    Case,
    When,
    Value,
    IntegerField,
    Count,
    Q,
    F,
    ProtectedError,
    Sum,
    Prefetch,
)
from django.db.models.functions import Now
from django.forms import HiddenInput
from django.http import Http404, HttpResponse, JsonResponse
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse, reverse_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import ListView
from django.views.generic.edit import (
    ModelFormMixin,
)
from github.GithubException import GithubException
from reversion.models import Version, Revision

from api.filters import (
    InstructorsOverTimeFilter,
    WorkshopsOverTimeFilter,
    LearnersOverTimeFilter,
)
from api.views import ReportsViewSet
from workshops.base_views import (
    AMYCreateView,
    AMYUpdateView,
    AMYDeleteView,
    AMYListView,
    RedirectSupportMixin,
    PrepopulationSupportMixin,
    AMYDetailView,
    StateFilterMixin,
)
from workshops.filters import (
    EventFilter,
    OrganizationFilter,
    MembershipFilter,
    MembershipTrainingsFilter,
    PersonFilter,
    TaskFilter,
    AirportFilter,
    EventRequestFilter,
    BadgeAwardsFilter,
    InvoiceRequestFilter,
    EventSubmissionFilter,
    DCSelfOrganizedEventRequestFilter,
    TraineeFilter,
    TrainingRequestFilter,
)
from workshops.forms import (
    SearchForm,
    DebriefForm,
    WorkshopStaffForm,
    PersonForm,
    PersonBulkAddForm,
    EventForm,
    TaskForm,
    AwardForm,
    PersonPermissionsForm,
    PersonsSelectionForm,
    OrganizationForm,
    PersonLookupForm,
    SimpleTodoForm,
    BootstrapHelper,
    AdminLookupForm,
    ProfileUpdateRequestFormNoCaptcha,
    MembershipForm,
    TodoFormSet,
    EventsSelectionForm,
    EventsMergeForm,
    InvoiceRequestUpdateForm,
    EventSubmitFormNoCaptcha,
    PersonsMergeForm,
    PersonCreateForm,
    SponsorshipForm,
    AutoUpdateProfileForm,
    DCSelfOrganizedEventRequestFormNoCaptcha,
    TrainingProgressForm,
    BulkAddTrainingProgressForm,
    MatchTrainingRequestForm,
    TrainingRequestUpdateForm,
    TrainingRequestsSelectionForm,
    TrainingRequestsMergeForm,
    SendHomeworkForm,
    BulkDiscardProgressesForm,
    BootstrapHelper,
    bootstrap_helper_inline_formsets,
    BulkChangeTrainingRequestForm,
    BulkMatchTrainingRequestForm,
    AllActivityOverTimeForm,
    ActionRequiredPrivacyForm,
    SWCEventRequestNoCaptchaForm,
    DCEventRequestNoCaptchaForm,
)
from workshops.management.commands.check_for_workshop_websites_updates import (
    Command as WebsiteUpdatesCommand,
)
from workshops.models import (
    Airport,
    Award,
    Badge,
    Event,
    Qualification,
    Person,
    Role,
    Organization,
    Membership,
    Sponsorship,
    Tag,
    Task,
    EventRequest,
    ProfileUpdateRequest,
    TodoItem,
    TodoItemQuerySet,
    InvoiceRequest,
    EventSubmission as EventSubmissionModel,
    TrainingRequest,
    DCSelfOrganizedEventRequest as DCSelfOrganizedEventRequestModel,
    is_admin,
    TrainingProgress,
    TrainingRequirement,
)
from workshops.util import (
    upload_person_task_csv,
    verify_upload_person_task,
    create_uploaded_persons_tasks,
    InternalError,
    WrongWorkshopURL,
    fetch_event_metadata,
    parse_metadata_from_event_website,
    validate_metadata_from_event_website,
    assignment_selection,
    get_pagination_items,
    failed_to_delete,
    assign,
    merge_objects,
    create_username,
    admin_required,
    OnlyForAdminsMixin,
    login_required,
    redirect_with_next_support,
    dict_without_Nones,
)


@login_required
def logout_then_login_with_msg(request):
    messages.success(request, 'You were successfully logged-out.')
    return logout_then_login(request)


@login_required
def dispatch(request):
    if is_admin(request.user):
        return redirect(reverse('admin-dashboard'))
    else:
        return redirect(reverse('trainee-dashboard'))


@admin_required
def admin_dashboard(request):
    """Home page for admins."""

    current_events = (
        Event.objects.upcoming_events() | Event.objects.ongoing_events()
    ).active().prefetch_related('tags')

    # This annotation may produce wrong number of instructors when
    # `unpublished_events` filters out events that contain a specific tag.
    # The bug was fixed in #1130.
    unpublished_events = Event.objects \
        .active().unpublished_events().select_related('host').annotate(
            num_instructors=Count(
                Case(
                    When(task__role__name='instructor', then=Value(1)),
                    output_field=IntegerField()
                )
            ),
        )

    assigned_to, is_admin = assignment_selection(request)

    if assigned_to == 'me':
        current_events = current_events.filter(assigned_to=request.user)
        unpublished_events = unpublished_events.filter(
            assigned_to=request.user)

    elif assigned_to == 'noone':
        current_events = current_events.filter(assigned_to__isnull=True)
        unpublished_events = unpublished_events.filter(
            assigned_to__isnull=True)

    elif assigned_to == 'all':
        # no filtering
        pass

    else:
        # no filtering
        pass

    # assigned events that have unaccepted changes
    updated_metadata = Event.objects.active() \
                                    .filter(assigned_to=request.user) \
                                    .filter(metadata_changed=True) \
                                    .count()

    context = {
        'title': None,
        'is_admin': is_admin,
        'assigned_to': assigned_to,
        'current_events': current_events,
        'unpublished_events': unpublished_events,
        'todos_start_date': TodoItemQuerySet.current_week_dates()[0],
        'todos_end_date': TodoItemQuerySet.next_week_dates()[1],
        'updated_metadata': updated_metadata,
        'carpentries': Tag.objects.carpentries(),
    }
    return render(request, 'workshops/admin_dashboard.html', context)


@admin_required
def changes_log(request):
    log = Revision.objects.all().select_related('user') \
                                .prefetch_related('version_set') \
                                .order_by('-date_created')
    log = get_pagination_items(request, log)
    context = {
        'log': log
    }
    return render(request, 'workshops/changes_log.html', context)


#------------------------------------------------------------


class AllOrganizations(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_organizations'
    template_name = 'workshops/all_organizations.html'
    filter_class = OrganizationFilter
    queryset = Organization.objects.prefetch_related(Prefetch(
        'membership_set',
        to_attr='current_memberships',
        queryset=Membership.objects.filter(
            agreement_start__lte=Now(),
            agreement_end__gte=Now(),
        )
    ))
    title = 'All Organizations'


class OrganizationDetails(OnlyForAdminsMixin, AMYDetailView):
    queryset = Organization.objects.all()
    context_object_name = 'organization'
    template_name = 'workshops/organization.html'
    slug_field = 'domain'
    slug_url_kwarg = 'org_domain'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Organization {0}'.format(self.object)
        return context


class OrganizationCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                         AMYCreateView):
    permission_required = 'workshops.add_organization'
    model = Organization
    form_class = OrganizationForm


class OrganizationUpdate(OnlyForAdminsMixin, PermissionRequiredMixin,
                         AMYUpdateView):
    permission_required = 'workshops.change_organization'
    model = Organization
    form_class = OrganizationForm
    slug_field = 'domain'
    slug_url_kwarg = 'org_domain'


class OrganizationDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                         AMYDeleteView):
    model = Organization
    slug_field = 'domain'
    slug_url_kwarg = 'org_domain'
    permission_required = 'workshops.delete_organization'
    success_url = reverse_lazy('all_organizations')


#------------------------------------------------------------


class AllMemberships(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_memberships'
    template_name = 'workshops/all_memberships.html'
    filter_class = MembershipFilter
    queryset = Membership.objects.annotate(
        instructor_training_seats_total=(
            F('seats_instructor_training') +
            F('additional_instructor_training_seats')
        ),
        # for future reference, in case someone would want to implement
        # this annotation
        # instructor_training_seats_utilized=(
        #     Count('task', filter=Q(task__role__name='learner'))
        # ),
        instructor_training_seats_remaining=(
            F('seats_instructor_training') +
            F('additional_instructor_training_seats') -
            Count('task', filter=Q(task__role__name='learner'))
        ),
    )
    title = 'All Memberships'


class MembershipDetails(OnlyForAdminsMixin, AMYDetailView):
    queryset = (
        Membership.objects
                  .select_related('organization')
                  .prefetch_related('task_set')
    )
    context_object_name = 'membership'
    template_name = 'workshops/membership.html'
    pk_url_kwarg = 'membership_id'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = '{0}'.format(self.object)
        return context


class MembershipCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                       PrepopulationSupportMixin, AMYCreateView):
    permission_required = [
        'workshops.add_membership',
        'workshops.change_organization',
    ]
    model = Membership
    form_class = MembershipForm
    populate_fields = ['organization']


class MembershipUpdate(OnlyForAdminsMixin, PermissionRequiredMixin,
                       RedirectSupportMixin, AMYUpdateView):
    permission_required = 'workshops.change_membership'
    model = Membership
    form_class = MembershipForm
    pk_url_kwarg = 'membership_id'


class MembershipDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                       AMYDeleteView):
    model = Membership
    permission_required = 'workshops.delete_membership'
    pk_url_kwarg = 'membership_id'

    def get_success_url(self):
        return reverse('organization_details', args=[
            self.get_object().organization.domain])

#------------------------------------------------------------

AIRPORT_FIELDS = ['iata', 'fullname', 'country', 'latitude', 'longitude']


class AllAirports(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_airports'
    queryset = Airport.objects.all()
    filter_class = AirportFilter
    template_name = 'workshops/all_airports.html'
    title = 'All Airports'


class AirportDetails(OnlyForAdminsMixin, AMYDetailView):
    queryset = Airport.objects.all()
    context_object_name = 'airport'
    template_name = 'workshops/airport.html'
    slug_url_kwarg = 'airport_iata'
    slug_field = 'iata'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Airport {0}'.format(self.object)
        return context


class AirportCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                    AMYCreateView):
    permission_required = 'workshops.add_airport'
    model = Airport
    fields = AIRPORT_FIELDS


class AirportUpdate(OnlyForAdminsMixin, PermissionRequiredMixin,
                    AMYUpdateView):
    permission_required = 'workshops.change_airport'
    model = Airport
    fields = AIRPORT_FIELDS
    slug_field = 'iata'
    slug_url_kwarg = 'airport_iata'


class AirportDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                    AMYDeleteView):
    model = Airport
    slug_field = 'iata'
    slug_url_kwarg = 'airport_iata'
    permission_required = 'workshops.delete_airport'
    success_url = reverse_lazy('all_airports')

#------------------------------------------------------------


class AllPersons(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_persons'
    template_name = 'workshops/all_persons.html'
    filter_class = PersonFilter
    queryset = Person.objects.defer('notes').annotate(
        is_swc_instructor=Sum(Case(When(badges__name='swc-instructor',
                                        then=1),
                                   default=0,
                                   output_field=IntegerField())),
        is_dc_instructor=Sum(Case(When(badges__name='dc-instructor',
                                       then=1),
                                  default=0,
                                  output_field=IntegerField())),
    )
    title = 'All Persons'


class PersonDetails(OnlyForAdminsMixin, AMYDetailView):
    context_object_name = 'person'
    template_name = 'workshops/person.html'
    pk_url_kwarg = 'person_id'
    queryset = Person.objects.annotate(
        num_taught=Count(
            Case(
                When(task__role__name='instructor', then=Value(1)),
                output_field=IntegerField()
            )
        ),
        num_helper=Count(
            Case(
                When(task__role__name='helper', then=Value(1)),
                output_field=IntegerField()
            )
        ),
        num_learner=Count(
            Case(
                When(task__role__name='learner', then=Value(1)),
                output_field=IntegerField()
            )
        )
    ).prefetch_related(
        'award_set__badge', 'award_set__awarded_by', 'award_set__event',
        'task_set__role', 'task_set__event',
    ).select_related('airport')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['title'] = 'Person {0}'.format(self.object)

        is_usersocialauth_in_sync = (
            len(self.object.github_usersocialauth) > 0
        )
        context['is_usersocialauth_in_sync'] = is_usersocialauth_in_sync

        return context


@admin_required
def person_bulk_add_template(request):
    ''' Dynamically generate a CSV template that can be used to bulk-upload
    people.

    See https://docs.djangoproject.com/en/1.7/howto/outputting-csv/#using-the-python-csv-library
    '''
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename=BulkPersonAddTemplate.csv'

    writer = csv.writer(response)
    writer.writerow(Person.PERSON_TASK_UPLOAD_FIELDS)
    return response


@admin_required
@permission_required(['workshops.add_person', 'workshops.change_person'],
                     raise_exception=True)
def person_bulk_add(request):
    if request.method == 'POST':
        form = PersonBulkAddForm(request.POST, request.FILES)
        if form.is_valid():
            charset = request.FILES['file'].charset or settings.DEFAULT_CHARSET
            stream = io.TextIOWrapper(request.FILES['file'].file, charset)
            try:
                persons_tasks, empty_fields = upload_person_task_csv(stream)
            except csv.Error as e:
                messages.error(
                    request,
                    "Error processing uploaded .CSV file: {}".format(e))
            except UnicodeDecodeError as e:
                messages.error(
                    request,
                    "Please provide a file in {} encoding."
                    .format(charset))
            else:
                if empty_fields:
                    msg_template = ("The following required fields were not"
                                    " found in the uploaded file: {}")
                    msg = msg_template.format(', '.join(empty_fields))
                    messages.error(request, msg)
                else:
                    # instead of insta-saving, put everything into session
                    # then redirect to confirmation page which in turn saves
                    # the data
                    request.session['bulk-add-people'] = persons_tasks
                    # request match
                    request.session['bulk-add-people-match'] = True
                    return redirect('person_bulk_add_confirmation')

    else:
        form = PersonBulkAddForm()

    context = {
        'title': 'Bulk Add People',
        'form': form,
        'charset': settings.DEFAULT_CHARSET,
        'roles': Role.objects.all()
    }
    return render(request, 'workshops/person_bulk_add_form.html', context)


@admin_required
@permission_required(['workshops.add_person', 'workshops.change_person'],
                     raise_exception=True)
def person_bulk_add_confirmation(request):
    """
    This view allows for manipulating and saving session-stored upload data.
    """
    persons_tasks = request.session.get('bulk-add-people')
    match = request.session.get('bulk-add-people-match', False)

    # if the session is empty, add message and redirect
    if not persons_tasks:
        messages.warning(request, "Could not locate CSV data, please "
                                  "upload again.")
        return redirect('person_bulk_add')

    if request.method == 'POST':
        # update values if user wants to change them
        personals = request.POST.getlist("personal")
        families = request.POST.getlist("family")
        usernames = request.POST.getlist("username")
        emails = request.POST.getlist("email")
        events = request.POST.getlist("event")
        roles = request.POST.getlist("role")
        data_update = zip(personals, families, usernames, emails, events,
                          roles)
        for k, record in enumerate(data_update):
            personal, family, username, email, event, role = record
            existing_person_id = persons_tasks[k].get('existing_person_id')
            # "field or None" converts empty strings to None values
            persons_tasks[k] = {
                'personal': personal,
                'family': family,
                'username': username,
                'email': email or None,
                'existing_person_id': existing_person_id,
            }
            # when user wants to drop related event they will send empty string
            # so we should unconditionally accept new value for event even if
            # it's an empty string
            persons_tasks[k]['event'] = event
            persons_tasks[k]['role'] = role
            persons_tasks[k]['errors'] = None  # reset here

        # save updated data to the session
        request.session['bulk-add-people'] = persons_tasks

        # check if user wants to verify or save, or cancel
        if request.POST.get('verify', None):
            # if there's "verify" in POST, then do only verification
            any_errors = verify_upload_person_task(persons_tasks)
            if any_errors:
                messages.error(request,
                                     "Please make sure to fix all errors "
                                     "listed below.")

        # there must be "confirm" and no "cancel" in POST in order to save
        elif (request.POST.get('confirm', None) and
              not request.POST.get('cancel', None)):
            try:
                # verification now makes something more than database
                # constraints so we should call it first
                verify_upload_person_task(persons_tasks)
                persons_created, tasks_created = \
                    create_uploaded_persons_tasks(persons_tasks)
            except (IntegrityError, ObjectDoesNotExist, InternalError) as e:
                messages.error(request,
                                     "Error saving data to the database: {}. "
                                     "Please make sure to fix all errors "
                                     "listed below.".format(e))
                any_errors = verify_upload_person_task(persons_tasks)

            else:
                request.session['bulk-add-people'] = None
                messages.success(
                    request,
                    'Successfully created {0} persons and {1} tasks.'
                    .format(len(persons_created), len(tasks_created))
                )
                return redirect('person_bulk_add')

        else:
            # any "cancel" or no "confirm" in POST cancels the upload
            request.session['bulk-add-people'] = None
            return redirect('person_bulk_add')

    else:
        # alters persons_tasks via reference
        any_errors = verify_upload_person_task(persons_tasks,
                                               match=bool(match))
        request.session['bulk-add-people-match'] = False

    roles = Role.objects.all().values_list('name', flat=True)

    context = {
        'title': 'Confirm uploaded data',
        'persons_tasks': persons_tasks,
        'any_errors': any_errors,
        'possible_roles': roles,
    }
    return render(request, 'workshops/person_bulk_add_results.html',
                  context)


@admin_required
@permission_required(['workshops.add_person', 'workshops.change_person'],
                     raise_exception=True)
def person_bulk_add_remove_entry(request, entry_id):
    "Remove specific entry from the session-saved list of people to be added."
    persons_tasks = request.session.get('bulk-add-people')

    if persons_tasks:
        entry_id = int(entry_id)
        try:
            del persons_tasks[entry_id]
            request.session['bulk-add-people'] = persons_tasks

        except IndexError:
            messages.warning(request, 'Could not find specified entry #{}'
                                      .format(entry_id))

        return redirect(person_bulk_add_confirmation)

    else:
        messages.warning(request, 'Could not locate CSV data, please try the '
                                  'upload again.')
        return redirect('person_bulk_add')


@admin_required
@permission_required(['workshops.add_person', 'workshops.change_person'],
                     raise_exception=True)
def person_bulk_add_match_person(request, entry_id, person_id=None):
    """Save information about matched person in the session-saved data."""
    persons_tasks = request.session.get('bulk-add-people')
    if not persons_tasks:
        messages.warning(request, 'Could not locate CSV data, please try the '
                                  'upload again.')
        return redirect('person_bulk_add')


    if person_id is None:
        # unmatch
        try:
            entry_id = int(entry_id)

            persons_tasks[entry_id]['existing_person_id'] = 0
            request.session['bulk-add-people'] = persons_tasks

        except ValueError:
            # catches invalid argument for int()
            messages.warning(request, 'Invalid entry ID ({}) or person ID '
                                      '({}).'.format(entry_id, person_id))

        except IndexError:
            # catches index out of bound
            messages.warning(request, 'Could not find specified entry #{}'
                                      .format(entry_id))

        return redirect(person_bulk_add_confirmation)

    else:
        # match
        try:
            entry_id = int(entry_id)
            person_id = int(person_id)

            persons_tasks[entry_id]['existing_person_id'] = person_id
            request.session['bulk-add-people'] = persons_tasks

        except ValueError:
            # catches invalid argument for int()
            messages.warning(request, 'Invalid entry ID ({}) or person ID '
                                      '({}).'.format(entry_id, person_id))

        except IndexError:
            # catches index out of bound
            messages.warning(request, 'Could not find specified entry #{}'
                                      .format(entry_id))

        return redirect(person_bulk_add_confirmation)


class PersonCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                   AMYCreateView):
    permission_required = 'workshops.add_person'
    model = Person
    form_class = PersonCreateForm

    def form_valid(self, form):
        """Person.lessons uses an intermediary model so we need to manually add
        objects of that model.

        See more here: http://stackoverflow.com/a/15745652"""
        self.object = form.save(commit=False)  # don't save M2M fields

        self.object.username = create_username(
            personal=form.cleaned_data['personal'],
            family=form.cleaned_data['family'])

        # Need to save that object because of commit=False previously.
        # This doesn't save our troublesome M2M field.
        self.object.save()

        # saving intermediary M2M model: Qualification
        for lesson in form.cleaned_data['lessons']:
            Qualification.objects.create(lesson=lesson, person=self.object)

        # Important: we need to use ModelFormMixin.form_valid() here!
        # But by doing so we omit SuccessMessageMixin completely, so we need to
        # simulate it.  The code below is almost identical to
        # SuccessMessageMixin.form_valid().
        response = super(ModelFormMixin, self).form_valid(form)
        success_message = self.get_success_message(form.cleaned_data)
        if success_message:
            messages.success(self.request, success_message)
        return response


class PersonUpdate(OnlyForAdminsMixin, UserPassesTestMixin,
                   AMYUpdateView):
    model = Person
    form_class = PersonForm
    pk_url_kwarg = 'person_id'
    template_name = 'workshops/person_edit_form.html'

    def test_func(self):
        if not (self.request.user.has_perm('workshops.change_person') or \
            self.request.user == self.get_object()):
            raise PermissionDenied
        return True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        kwargs = {
            'initial': {'person': self.object},
            'widgets': {'person': HiddenInput()},
        }
        context.update({
            'awards': self.object.award_set.select_related('event', 'badge')
                          .order_by('badge__name'),
            'award_form': AwardForm(**kwargs),
            'tasks': self.object.task_set.select_related('role', 'event')
                         .order_by('-event__slug'),
            'task_form': TaskForm(**kwargs),
        })
        return context

    def form_valid(self, form):
        self.object = form.save(commit=False)
        # remove existing Qualifications for user
        Qualification.objects.filter(person=self.object).delete()
        # add new Qualifications
        for lesson in form.cleaned_data.pop('lessons'):
            Qualification.objects.create(person=self.object, lesson=lesson)
        return super().form_valid(form)


class PersonDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                   AMYDeleteView):
    model = Person
    permission_required = 'workshops.delete_person'
    success_url = reverse_lazy('all_persons')
    pk_url_kwarg = 'person_id'


class PersonPermissions(OnlyForAdminsMixin, PermissionRequiredMixin,
                        AMYUpdateView):
    permission_required = 'workshops.change_person'
    model = Person
    form_class = PersonPermissionsForm
    pk_url_kwarg = 'person_id'


@login_required
def person_password(request, person_id):
    user = get_object_or_404(Person, pk=person_id)

    # Either the user requests change of their own password, or someone with
    # permission for changing person does.
    if not ((request.user == user) or
            (request.user.has_perm('workshops.change_person'))):
        raise PermissionDenied

    Form = PasswordChangeForm
    if request.user.is_superuser:
        Form = SetPasswordForm
    elif request.user.pk != user.pk:
        # non-superuser can only change their own password, not someone else's
        raise PermissionDenied

    if request.method == 'POST':
        form = Form(user, request.POST)
        if form.is_valid():
            form.save()  # saves the password for the user

            update_session_auth_hash(request, form.user)

            messages.success(request, 'Password was changed successfully.')

            return redirect(reverse('person_details', args=[user.id]))

        else:
            messages.error(request, 'Fix errors below.')
    else:
        form = Form(user)

    form.helper = BootstrapHelper(add_cancel_button=False)
    return render(request, 'workshops/generic_form.html', {
        'form': form,
        'model': Person,
        'object': user,
        'title': 'Change password',
    })


@admin_required
@permission_required(['workshops.delete_person', 'workshops.change_person'],
                     raise_exception=True)
def persons_merge(request):
    """Display two persons side by side on GET and merge them on POST.

    If no persons are supplied via GET params, display person selection
    form."""
    obj_a_pk = request.GET.get('person_a')
    obj_b_pk = request.GET.get('person_b')

    if not obj_a_pk or not obj_b_pk:
        context = {
            'title': 'Merge Persons',
            'form': PersonsSelectionForm(),
        }
        return render(request, 'workshops/generic_form.html', context)

    obj_a = get_object_or_404(Person, pk=obj_a_pk)
    obj_b = get_object_or_404(Person, pk=obj_b_pk)

    form = PersonsMergeForm(initial=dict(person_a=obj_a, person_b=obj_b))

    if request.method == 'POST':
        form = PersonsMergeForm(request.POST)

        if form.is_valid():
            # merging in process
            data = form.cleaned_data

            obj_a = data['person_a']
            obj_b = data['person_b']

            # `base_obj` stays in the database after merge
            # `merging_obj` will be removed from DB after merge
            if data['id'] == 'obj_a':
                base_obj = obj_a
                merging_obj = obj_b
                base_a = True
            else:
                base_obj = obj_b
                merging_obj = obj_a
                base_a = False

            # non-M2M-relationships
            easy = (
                'username', 'personal', 'middle', 'family', 'email',
                'may_contact', 'publish_profile', 'gender', 'airport',
                'github', 'twitter', 'url', 'notes', 'affiliation',
                'occupation', 'orcid', 'is_active',
            )

            # M2M relationships
            difficult = ('award_set', 'qualification_set', 'domains',
                         'languages', 'task_set', 'trainingprogress_set')

            try:
                _, integrity_errors = merge_objects(obj_a, obj_b, easy,
                                                    difficult, choices=data,
                                                    base_a=base_a)

                if integrity_errors:
                    msg = ('There were integrity errors when merging related '
                           'objects:\n' '\n'.join(integrity_errors))
                    messages.warning(request, msg)

            except ProtectedError as e:
                return failed_to_delete(request, object=merging_obj,
                                        protected_objects=e.protected_objects)

            else:
                messages.success(request, 'Persons were merged successfully. '
                                          'You were redirected to the base '
                                          'person.')
                return redirect(base_obj.get_absolute_url())
        else:
            messages.error(request, 'Fix errors in the form.')

    context = {
        'title': 'Merge two persons',
        'form': form,
        'obj_a': obj_a,
        'obj_b': obj_b,
    }
    return render(request, 'workshops/persons_merge.html', context)


@admin_required
def sync_usersocialauth(request, person_id):
    person_id = int(person_id)
    try:
        person = Person.objects.get(pk=person_id)
    except Person.DoesNotExist:
        messages.error(request,
                       'Cannot sync UserSocialAuth table for person #{} '
                       '-- there is no Person with such id.'.format(person_id))
        return redirect(reverse('persons'))
    else:
        try:
            result = person.synchronize_usersocialauth()
            if result:
                messages.success(
                    request, 'Social account was successfully synchronized.')
            else:
                messages.error(
                    request, 'It was not possible to synchronize this person '
                             'with their social account.')

        except GithubException:
            messages.error(request,
                           'Cannot sync UserSocialAuth table for person #{} '
                           'due to errors with GitHub API.'.format(person_id))

        return redirect(reverse('person_details', args=(person_id,)))

#------------------------------------------------------------


class AllEvents(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_events'
    template_name = 'workshops/all_events.html'
    queryset = (
        Event.objects
        .defer('notes')
        .select_related('assigned_to')
        .prefetch_related('host', 'tags')
        .annotate(
            num_instructors=Sum(
                Case(When(task__role__name='instructor', then=Value(1)),
                     default=0,
                     output_field=IntegerField()),
            )
        )
    )
    filter_class = EventFilter
    title = 'All Events'


@admin_required
def event_details(request, slug):
    '''List details of a particular event.'''
    try:
        sponsorship_prefetch = Prefetch(
            'sponsorship_set',
            queryset=Sponsorship.objects.select_related('contact')
        )
        task_prefetch = Prefetch(
            'task_set',
            to_attr='contacts',
            queryset=Task.objects.select_related('person').filter(
                # we only want hosts, organizers and instructors
                Q(role__name='host') | Q(role__name='organizer') |
                Q(role__name='instructor')
            ).filter(person__may_contact=True)
            .exclude(Q(person__email='') | Q(person__email=None))
        )
        event = (
            Event.objects
                 .prefetch_related(sponsorship_prefetch, task_prefetch)
                 .select_related('eventrequest', 'eventsubmission',
                                 'dcselforganizedeventrequest', 'assigned_to',
                                 'host', 'administrator').get(slug=slug)
        )
        member_sites = (
            Membership.objects.filter(task__event=event)
                              .distinct()
        )
    except Event.DoesNotExist:
        raise Http404('Event matching query does not exist.')

    person_instructor_badges = Prefetch(
        'person__badges',
        to_attr='person_instructor_badges',
        queryset=Badge.objects.filter(name__in=Badge.INSTRUCTOR_BADGES)
    )
    tasks = (
        Task.objects
            .filter(event__id=event.id)
            .select_related('event', 'person', 'role')
            .prefetch_related(person_instructor_badges)
            .order_by('role__name')
    )
    todos = event.todoitem_set.all()
    todo_form = SimpleTodoForm(prefix='todo', initial={
        'event': event,
    })

    if request.method == "POST" and request.user.has_perm('workshops.add_todoitem'):
        # Create ToDo items on todo_form submission only when user has permission
        todo_form = SimpleTodoForm(request.POST, prefix='todo', initial={
            'event': event,
        })
        if todo_form.is_valid():
            todo = todo_form.save()

            messages.success(
                request,
                'New TODO {todo} was added to the event {event}.'.format(
                    todo=str(todo),
                    event=event.slug,
                ),
                extra_tags='newtodo',
            )
            return redirect(reverse(event_details, args=[slug, ]))
        else:
            messages.error(request, 'Fix errors in the TODO form.',
                           extra_tags='todos')

    admin_lookup_form = AdminLookupForm()
    if event.assigned_to:
        admin_lookup_form = AdminLookupForm(
            initial={'person': event.assigned_to}
        )

    admin_lookup_form.helper = BootstrapHelper(
        form_action=reverse('event_assign', args=[slug]),
        add_cancel_button=False)

    context = {
        'title': 'Event {0}'.format(event),
        'event': event,
        'tasks': tasks,
        'member_sites': member_sites,
        'todo_form': todo_form,
        'todos': todos,
        'all_emails' : tasks.filter(person__may_contact=True)\
            .exclude(person__email=None)\
            .values_list('person__email', flat=True),
        'today': datetime.date.today(),
        'admin_lookup_form': admin_lookup_form,
    }
    return render(request, 'workshops/event.html', context)


@admin_required
def validate_event(request, slug):
    '''Check the event's home page *or* the specified URL (for testing).'''
    try:
        event = Event.objects.get(slug=slug)
    except Event.DoesNotExist:
        raise Http404('Event matching query does not exist.')

    page_url = request.GET.get('url', None)  # for manual override
    if page_url is None:
        page_url = event.url

    page_url = page_url.strip()

    error_messages = []
    warning_messages = []

    try:
        metadata = fetch_event_metadata(page_url)
        # validate metadata
        error_messages, warning_messages = \
            validate_metadata_from_event_website(metadata)

    except WrongWorkshopURL as e:
        error_messages.append(str(e))

    except requests.exceptions.HTTPError as e:
        error_messages.append(
            'Request for "{0}" returned status code {1}'
            .format(page_url, e.response.status_code)
        )

    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        error_messages.append("Network connection error.")

    context = {
        'title': 'Validate Event {0}'.format(event),
        'event': event,
        'page': page_url,
        'error_messages': error_messages,
        'warning_messages': warning_messages,
    }
    return render(request, 'workshops/validate_event.html', context)


class EventCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                  AMYCreateView):
    permission_required = 'workshops.add_event'
    model = Event
    form_class = EventForm
    template_name = 'workshops/event_create_form.html'


class EventUpdate(OnlyForAdminsMixin, PermissionRequiredMixin,
                  AMYUpdateView):
    permission_required = [
        'workshops.change_event',
        'workshops.add_task',
        'workshops.add_sponsorship',
    ]
    queryset = Event.objects.select_related(
        'assigned_to', 'administrator', 'language', 'eventrequest',
        'eventsubmission', 'dcselforganizedeventrequest'
    ).prefetch_related('sponsorship_set')
    slug_field = 'slug'
    form_class = EventForm
    template_name = 'workshops/event_edit_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        kwargs = {
            'initial': {'event': self.object},
            'widgets': {'event': HiddenInput()},
        }
        context.update({
            'tasks': self.get_object().task_set
                        .select_related('person', 'role')
                        .order_by('role__name'),
            'task_form': TaskForm(**kwargs),
            'sponsor_form': SponsorshipForm(**kwargs),
        })
        return context


class EventDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                  AMYDeleteView):
    model = Event
    permission_required = 'workshops.delete_event'
    success_url = reverse_lazy('all_events')


@admin_required
def event_import(request):
    """Read metadata from remote URL and return them as JSON.

    This is used to read metadata from workshop website and then fill up fields
    on event_create form."""

    url = request.GET.get('url', '').strip()

    try:
        metadata = fetch_event_metadata(url)
        # normalize the metadata
        metadata = parse_metadata_from_event_website(metadata)
        return JsonResponse(metadata)

    except requests.exceptions.HTTPError as e:
        return HttpResponseBadRequest(
            'Request for "{0}" returned status code {1}.'
            .format(url, e.response.status_code)
        )

    except requests.exceptions.RequestException:
        return HttpResponseBadRequest('Network connection error.')

    except WrongWorkshopURL as e:
        return HttpResponseBadRequest(str(e))

    except KeyError:
        return HttpResponseBadRequest('Missing or wrong "url" parameter.')


@admin_required
@permission_required('workshops.change_event', raise_exception=True)
def event_assign(request, slug, person_id=None):
    """Set event.assigned_to. See `assign` docstring for more information."""
    try:
        event = Event.objects.get(slug=slug)

        assign(request, event, person_id)

        return redirect(reverse('event_details', args=[event.slug]))

    except Event.DoesNotExist:
        raise Http404("No event found matching the query.")


@admin_required
@permission_required(['workshops.delete_event', 'workshops.change_event'],
                     raise_exception=True)
def events_merge(request):
    """Display two events side by side on GET and merge them on POST.

    If no events are supplied via GET params, display event selection form."""
    obj_a_pk = request.GET.get('event_a')
    obj_b_pk = request.GET.get('event_b')

    if not obj_a_pk and not obj_b_pk:
        context = {
            'title': 'Merge Events',
            'form': EventsSelectionForm(),
        }
        return render(request, 'workshops/generic_form.html', context)

    obj_a = get_object_or_404(Event, pk=obj_a_pk)
    obj_b = get_object_or_404(Event, pk=obj_b_pk)

    form = EventsMergeForm(initial=dict(event_a=obj_a, event_b=obj_b))

    if request.method == "POST":
        form = EventsMergeForm(request.POST)

        if form.is_valid():
            # merging in process
            data = form.cleaned_data

            obj_a = data['event_a']
            obj_b = data['event_b']

            # `base_obj` stays in the database after merge
            # `merging_obj` will be removed from DB after merge
            if data['id'] == 'obj_a':
                base_obj = obj_a
                merging_obj = obj_b
                base_a = True
            else:
                base_obj = obj_b
                merging_obj = obj_a
                base_a = False

            # non-M2M-relationships:
            easy = (
                'slug', 'completed', 'assigned_to', 'start', 'end', 'host',
                'administrator', 'url', 'language', 'reg_key', 'admin_fee',
                'invoice_status', 'attendance', 'contact', 'country', 'venue',
                'address', 'latitude', 'longitude', 'learners_pre',
                'learners_post', 'instructors_pre', 'instructors_post',
                'learners_longterm', 'notes',
            )
            # M2M relationships
            difficult = ('tags', 'task_set', 'todoitem_set')

            try:
                _, integrity_errors = merge_objects(obj_a, obj_b, easy,
                                                    difficult, choices=data,
                                                    base_a=base_a)

                if integrity_errors:
                    msg = ('There were integrity errors when merging related '
                           'objects:\n' '\n'.join(integrity_errors))
                    messages.warning(request, msg)

            except ProtectedError as e:
                return failed_to_delete(request, object=merging_obj,
                                        protected_objects=e.protected_objects)

            else:
                messages.success(request, 'Events were merged successfully. '
                                          'You were redirected to the base '
                                          'event.')
                return redirect(base_obj.get_absolute_url())
        else:
            messages.error(request, 'Fix errors in the form.')

    context = {
        'title': 'Merge two events',
        'obj_a': obj_a,
        'obj_b': obj_b,
        'form': form,
    }
    return render(request, 'workshops/events_merge.html', context)


# disabled as per @maneesha's request (disabled in HTML template)
# see https://github.com/swcarpentry/amy/issues/1040
@admin_required
@permission_required('workshops.add_invoicerequest', raise_exception=True)
def event_invoice(request, slug):
    # try:
    #     event = Event.objects.get(slug=slug)
    # except ObjectDoesNotExist:
    #     raise Http404("No event found matching the query.")

    # form = InvoiceRequestForm(initial=dict(
    #     organization=event.host, date=event.start, event=event,
    #     event_location=event.venue, amount=event.admin_fee,
    # ))

    # if request.method == 'POST':
    #     form = InvoiceRequestForm(request.POST)

    #     if form.is_valid():
    #         form.save()
    #         messages.success(request,
    #                          'Successfully added an invoice request for {}.'
    #                          .format(event.slug))
    #         return redirect(reverse('event_details',
    #                                 args=[event.slug]))
    #     else:
    #         messages.error(request, 'Fix errors below.')

    # context = {
    #     'title_left': 'Event {}'.format(event.slug),
    #     'title_right': 'New invoice request',
    #     'event': event,
    #     'form': form,
    # }
    context = {
        'title': 'Invoice',
    }
    return render(request, 'workshops/event_invoice.html', context)


@admin_required
def events_metadata_changed(request):
    """List events with metadata changed."""
    events = Event.objects.active().filter(metadata_changed=True)

    assigned_to, is_admin = assignment_selection(request)

    if assigned_to == 'me':
        events = events.filter(assigned_to=request.user)

    elif assigned_to == 'noone':
        events = events.filter(assigned_to=None)

    elif assigned_to == 'all':
        # no filtering
        pass

    else:
        # no filtering
        pass

    context = {
        'title': 'Events with metadata changed',
        'events': events,
        'is_admin': is_admin,
        'assigned_to': assigned_to,
    }
    return render(request, 'workshops/events_metadata_changed.html', context)


@admin_required
@permission_required('workshops.change_event', raise_exception=True)
def event_review_metadata_changes(request, slug):
    """Review changes made to metadata on event's website."""
    try:
        event = Event.objects.get(slug=slug)
    except Event.DoesNotExist:
        raise Http404('No event found matching the query.')

    try:
        metadata = fetch_event_metadata(event.website_url)
    except requests.exceptions.RequestException:
        messages.error(request, "There was an error while fetching event's "
                                "website. Make sure the event has website URL "
                                "provided, and that it's reachable.")
        return redirect(event.get_absolute_url())

    metadata = parse_metadata_from_event_website(metadata)

    # save serialized metadata in session so in case of acceptance we don't
    # reload them
    cmd = WebsiteUpdatesCommand()
    metadata_serialized = cmd.serialize(metadata)
    request.session['metadata_from_event_website'] = metadata_serialized

    context = {
        'title': 'Review changes for {}'.format(str(event)),
        'metadata': metadata,
        'event': event,
    }
    return render(request, 'workshops/event_review_metadata_changes.html',
                  context)


@admin_required
@permission_required('workshops.change_event', raise_exception=True)
def event_accept_metadata_changes(request, slug):
    """Review changes made to metadata on event's website."""
    try:
        event = Event.objects.get(slug=slug)
    except Event.DoesNotExist:
        raise Http404('No event found matching the query.')

    # load serialized metadata from session
    metadata_serialized = request.session.get('metadata_from_event_website')
    if not metadata_serialized:
        raise Http404('Nothing to update.')
    cmd = WebsiteUpdatesCommand()
    metadata = cmd.deserialize(metadata_serialized)

    # update values
    ALLOWED_METADATA = ('start', 'end', 'country', 'venue', 'address',
                        'latitude', 'longitude', 'contact', 'reg_key')
    for key, value in metadata.items():
        if hasattr(event, key) and key in ALLOWED_METADATA:
            setattr(event, key, value)

    # update instructors and helpers
    instructors = ', '.join(metadata.get('instructors', []))
    helpers = ', '.join(metadata.get('helpers', []))
    event.notes += (
        '\n\n---------\nUPDATE {:%Y-%m-%d}:'
        '\nINSTRUCTORS: {}\n\nHELPERS: {}'
        .format(datetime.date.today(), instructors, helpers)
    )

    # save serialized metadata
    event.repository_metadata = metadata_serialized

    # dismiss notification
    event.metadata_all_changes = ''
    event.metadata_changed = False
    event.save()

    # remove metadata from session
    del request.session['metadata_from_event_website']

    messages.success(request,
                     'Successfully updated {}.'.format(event.slug))

    return redirect(reverse('event_details', args=[event.slug]))


@admin_required
@permission_required('workshops.change_event', raise_exception=True)
def event_dismiss_metadata_changes(request, slug):
    """Review changes made to metadata on event's website."""
    try:
        event = Event.objects.get(slug=slug)
    except Event.DoesNotExist:
        raise Http404('No event found matching the query.')

    # dismiss notification
    event.metadata_all_changes = ''
    event.metadata_changed = False
    event.save()

    # remove metadata from session
    if 'metadata_from_event_website' in request.session:
        del request.session['metadata_from_event_website']

    messages.success(request,
                     'Changes to {} were dismissed.'.format(event.slug))

    return redirect(reverse('event_details', args=[event.slug]))


class SponsorshipCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                        AMYCreateView):
    model = Sponsorship
    permission_required = 'workshops.add_sponsorship'
    form_class = SponsorshipForm

    def get_success_url(self):
        return reverse('event_edit',args=[self.object.event.slug]) + '#sponsors'


class SponsorshipDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                        AMYDeleteView):
    model = Sponsorship
    permission_required = 'workshops.delete_sponsorship'

    def get_success_url(self):
        return reverse('event_edit', args=[self.get_object().event.slug]) + '#sponsors'


class AllInvoiceRequests(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'requests'
    template_name = 'workshops/all_invoicerequests.html'
    filter_class = InvoiceRequestFilter
    queryset = InvoiceRequest.objects.all()
    title = 'Invoice requests'

    def get_filter_data(self):
        data = self.request.GET.copy()
        data['status'] = data.get('status', '')
        return data


class InvoiceRequestDetails(OnlyForAdminsMixin, AMYDetailView):
    context_object_name = 'object'
    template_name = 'workshops/invoicerequest.html'
    queryset = InvoiceRequest.objects.all()
    pk_url_kwarg = 'request_id'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Invoice request #{}'.format(self.get_object().pk)
        return context


class InvoiceRequestUpdate(OnlyForAdminsMixin, PermissionRequiredMixin,
                           AMYUpdateView):
    permission_required = 'workshops.change_invoicerequest'
    model = InvoiceRequest
    form_class = InvoiceRequestUpdateForm
    pk_url_kwarg = 'request_id'


# ------------------------------------------------------------


class AllTasks(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_tasks'
    template_name = 'workshops/all_tasks.html'
    filter_class = TaskFilter
    queryset = Task.objects.select_related('event', 'person', 'role') \
                           .defer('person__notes', 'event__notes')
    title = 'All Tasks'


@admin_required
def task_details(request, task_id):
    '''List details of a particular task.'''
    task = get_object_or_404(Task, pk=task_id)
    context = {'title' : 'Task {0}'.format(task),
               'task' : task}
    return render(request, 'workshops/task.html', context)


class TaskCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                 RedirectSupportMixin, AMYCreateView):
    permission_required = 'workshops.add_task'
    model = Task
    form_class = TaskForm

    def post(self, request, *args, **kwargs):
        """Save request in `self.request`."""
        self.request = request
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        """Check associated membership remaining seats and validity."""
        seat_membership = form.cleaned_data['seat_membership']
        if hasattr(self, 'request') and seat_membership is not None:
            # check number of available seats
            if seat_membership.seats_instructor_training_remaining == 1:
                messages.warning(
                    self.request,
                    'Membership "{}" has 0 instructor training seats'
                    ' available.'.format(str(seat_membership))
                )
            if seat_membership.seats_instructor_training_remaining < 1:
                messages.warning(
                    self.request,
                    'Membership "{}" is using more training seats'
                    ' than it\'s been allowed.'.format(str(seat_membership))
                )

            today = datetime.date.today()
            # check if membership is active
            if not (seat_membership.agreement_start <= today <= seat_membership.agreement_end):
                messages.warning(
                    self.request,
                    'Membership "{}" is not active.'.format(str(seat_membership))
                )

        return super().form_valid(form)


class TaskUpdate(OnlyForAdminsMixin, PermissionRequiredMixin,
                 AMYUpdateView):
    permission_required = 'workshops.change_task'
    model = Task
    form_class = TaskForm
    pk_url_kwarg = 'task_id'


class TaskDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                 RedirectSupportMixin, AMYDeleteView):
    model = Task
    permission_required = 'workshops.delete_task'
    success_url = reverse_lazy('all_tasks')
    pk_url_kwarg = 'task_id'


#------------------------------------------------------------


class MockAwardCreate(OnlyForAdminsMixin, PermissionRequiredMixin,
                      PrepopulationSupportMixin, AMYCreateView):
    permission_required = 'workshops.add_award'
    model = Award
    form_class = AwardForm
    populate_fields = ['badge', 'person']

    def get_initial(self, **kwargs):
        initial = super().get_initial(**kwargs)

        # Determine initial event in AwardForm
        if 'find-training' in self.request.GET:
            tasks = Person.objects.get(
                pk=self.request.GET['person']
            ).get_training_tasks()
            if tasks.count() == 1:
                initial.update({'event': tasks[0].event})

        return initial

    def get_success_url(self):
        return reverse('badge_details', args=[self.object.badge.name])


class AwardCreate(RedirectSupportMixin, MockAwardCreate):
    pass


class MockAwardDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                      AMYDeleteView):
    model = Award
    permission_required = 'workshops.delete_award'

    def get_success_url(self):
        return reverse('badge_details', args=[self.get_object().badge.name])


class AwardDelete(RedirectSupportMixin, MockAwardDelete):
    # Modify the MRO to look like:
    # AwardDelete < RedirectSupportMixin < MockAwardDelete
    #
    # This ensures that `super()` when called from `get_success_url` method of
    # RedirectSupportMixin returns MockAwardDelete
    pass


#------------------------------------------------------------


class AllBadges(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_badges'
    queryset = Badge.objects.order_by('name') \
                            .annotate(num_awarded=Count('award'))
    template_name = 'workshops/all_badges.html'
    title = 'All Badges'


class BadgeDetails(OnlyForAdminsMixin, AMYDetailView):
    queryset = Badge.objects.all()
    context_object_name = 'badge'
    template_name = 'workshops/badge.html'
    slug_field = 'name'
    slug_url_kwarg = 'badge_name'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['title'] = 'Badge {0}'.format(self.object)
        filter = BadgeAwardsFilter(
            self.request.GET,
            queryset=self.object.award_set.select_related(
                'event', 'person', 'badge')
        )
        context['filter'] = filter

        awards = get_pagination_items(self.request, filter.qs)
        context['awards'] = awards

        return context


#------------------------------------------------------------


class AllTrainings(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'all_trainings'
    template_name = 'workshops/all_trainings.html'
    queryset = Event.objects.filter(tags__name='TTT').annotate(
        trainees=Count(Case(When(task__role__name='learner',
                                 then=F('task__person__id')),
                            output_field=IntegerField()),
                       distinct=True),
        finished=Count(Case(When(task__role__name='learner',
                                 task__person__badges__in=Badge.objects.instructor_badges(),
                                 then=F('task__person__id')),
                            output_field=IntegerField()),
                       distinct=True),
    ).exclude(trainees=0).order_by('-start')
    title = 'All Instructor Trainings'


#------------------------------------------------------------


@admin_required
def workshop_staff(request):
    '''Search for workshop staff.'''
    instructor_badges = Badge.objects.instructor_badges()
    TTT = Tag.objects.get(name='TTT')
    stalled = Tag.objects.get(name='stalled')

    people = Person.objects.filter(airport__isnull=False) \
                           .select_related('airport') \
                           .prefetch_related('badges', 'lessons')

    trainees = Task.objects.filter(event__tags=TTT) \
                           .filter(role__name='learner') \
                           .filter(person__airport__isnull=False) \
                           .exclude(event__tags=stalled) \
                           .exclude(person__badges__in=instructor_badges) \
                           .values_list('person__pk', flat=True)

    # we need to count number of specific roles users had
    # and if they are SWC/DC instructors
    people = people.annotate(
        num_taught=Count(
            Case(
                When(task__role__name='instructor', then=Value(1)),
                output_field=IntegerField()
            )
        ),
        num_helper=Count(
            Case(
                When(task__role__name='helper', then=Value(1)),
                output_field=IntegerField()
            )
        ),
        num_organizer=Count(
            Case(
                When(task__role__name='organizer', then=Value(1)),
                output_field=IntegerField()
            )
        )
    )

    filter_form = WorkshopStaffForm()

    lessons = list()

    if 'submit' in request.GET:
        filter_form = WorkshopStaffForm(request.GET)
        if filter_form.is_valid():
            data = filter_form.cleaned_data

            if data['lessons']:
                lessons = data['lessons']
                # this has to be in a loop to match a *subset* of lessons,
                # not any lesson within the list (as it would be with
                # `.filter(lessons_in=lessons)`)
                for lesson in lessons:
                    people = people.filter(
                        qualification__lesson=lesson
                    )

            if data['airport']:
                x = data['airport'].latitude
                y = data['airport'].longitude
                # using Euclidean distance just because it's faster and easier
                complex_F = ((F('airport__latitude') - x) ** 2 +
                             (F('airport__longitude') - y) ** 2)
                people = people.annotate(distance=complex_F) \
                               .order_by('distance', 'family')

            if data['latitude'] and data['longitude']:
                x = data['latitude']
                y = data['longitude']
                # using Euclidean distance just because it's faster and easier
                complex_F = ((F('airport__latitude') - x) ** 2 +
                             (F('airport__longitude') - y) ** 2)
                people = people.annotate(distance=complex_F) \
                               .order_by('distance', 'family')

            if data['country']:
                people = people.filter(
                    Q(airport__country__in=data['country']) |
                    Q(country__in=data['country'])
                ).order_by('family')

            if data['gender']:
                people = people.filter(gender=data['gender'])

            if data['instructor_badges']:
                for badge in data['instructor_badges']:
                    people = people.filter(badges__name=badge)

            # it's faster to count role=helper occurences than to check if user
            # had a role=helper
            if data['was_helper']:
                people = people.filter(num_helper__gte=1)

            if data['was_organizer']:
                people = people.filter(num_organizer__gte=1)

            if data['is_in_progress_trainee']:
                # filter out people who took part in only stalled TTT events
                TTT_non_stalled_events = (
                    Event.objects.exclude(tags=stalled).filter(tags=TTT)
                )
                q = Q(task__event__in=TTT_non_stalled_events)
                people = people.filter(q, task__role__name='learner') \
                               .exclude(badges__in=instructor_badges)

            if data['languages']:
                for language in data['languages']:
                    people = people.filter(languages=language)

    emails = people.filter(may_contact=True).values_list('email', flat=True)
    people = get_pagination_items(request, people)
    context = {
        'title': 'Find Workshop Staff',
        'filter_form': filter_form,
        'persons': people,
        'lessons': lessons,
        'instructor_badges': instructor_badges,
        'trainees': trainees,
        'emails': emails,
    }
    return render(request, 'workshops/workshop_staff.html', context)

#------------------------------------------------------------


@csrf_exempt
@admin_required
def search(request):
    '''Search the database by term.'''

    term = ''
    organizations = events = persons = airports = training_requests = None

    if request.method == 'GET':
        form = SearchForm(request.GET)
        if form.is_valid():
            term = form.cleaned_data['term']
            tokens = re.split('\s+', term)
            results = list()

            if form.cleaned_data['in_organizations']:
                organizations = Organization.objects.filter(
                    Q(domain__icontains=term) |
                    Q(fullname__icontains=term) |
                    Q(notes__icontains=term)) \
                    .order_by('fullname')
                results += list(organizations)

            if form.cleaned_data['in_events']:
                events = Event.objects.filter(
                    Q(slug__icontains=term) |
                    Q(notes__icontains=term) |
                    Q(host__domain__icontains=term) |
                    Q(host__fullname__icontains=term) |
                    Q(url__icontains=term) |
                    Q(contact__icontains=term) |
                    Q(venue__icontains=term) |
                    Q(address__icontains=term)
                ).order_by('-slug')
                results += list(events)

            if form.cleaned_data['in_persons']:
                # if user searches for two words, assume they mean a person
                # name
                if len(tokens) == 2:
                    name1, name2 = tokens
                    complex_q = (
                        Q(personal__icontains=name1) & Q(family__icontains=name2)
                    ) | (
                        Q(personal__icontains=name2) & Q(family__icontains=name1)
                    ) | Q(email__icontains=term) | Q(github__icontains=term)
                    persons = Person.objects.filter(complex_q)
                else:
                    persons = Person.objects.filter(
                        Q(personal__icontains=term) |
                        Q(family__icontains=term) |
                        Q(email__icontains=term) |
                        Q(github__icontains=term)) \
                        .order_by('family')
                results += list(persons)

            if form.cleaned_data['in_airports']:
                airports = Airport.objects.filter(
                    Q(iata__icontains=term) |
                    Q(fullname__icontains=term)) \
                    .order_by('iata')
                results += list(airports)

            if form.cleaned_data['in_training_requests']:
                training_requests = TrainingRequest.objects.filter(
                    Q(group_name__icontains=term) |
                    Q(family__icontains=term) |
                    Q(email__icontains=term) |
                    Q(github__icontains=term) |
                    Q(affiliation__icontains=term) |
                    Q(location__icontains=term) |
                    Q(comment__icontains=term)
                )
                results += list(training_requests)

            # only 1 record found? Let's move to it immediately
            if len(results) == 1:
                return redirect(results[0].get_absolute_url())

    # if empty GET, we'll create a blank form
    else:
        form = SearchForm()

    context = {
        'title': 'Search',
        'form': form,
        'term': term,
        'organizations' : organizations,
        'events': events,
        'persons': persons,
        'airports': airports,
        'training_requests': training_requests,
    }
    return render(request, 'workshops/search.html', context)

#------------------------------------------------------------

@admin_required
def export_badges(request):
    title = 'Export Badges'

    badges_api_link = reverse('api:export-badges')
    badges_json_link = reverse('api:export-badges', kwargs={'format': 'json'})
    badges_yaml_link = reverse('api:export-badges', kwargs={'format': 'yaml'})

    by_person_api_link = reverse('api:export-badges-by-person')
    by_person_json_link = reverse('api:export-badges-by-person',
                                  kwargs={'format': 'json'})
    by_person_yaml_link = reverse('api:export-badges-by-person',
                                  kwargs={'format': 'yaml'})
    context = {
        'title': title,
        'badges_api_link': badges_api_link,
        'badges_json_link': badges_json_link,
        'badges_yaml_link': badges_yaml_link,
        'by_person_api_link': by_person_api_link,
        'by_person_json_link': by_person_json_link,
        'by_person_yaml_link': by_person_yaml_link,
    }
    return render(request, 'workshops/export_badges.html', context)


@admin_required
def export_instructors(request):
    title = 'Instructor Locations'
    json_link = reverse('api:export-instructors', kwargs={'format': 'json'})
    yaml_link = reverse('api:export-instructors', kwargs={'format': 'yaml'})
    context = {
        'title': title,
        'json_link': json_link,
        'yaml_link': yaml_link,
    }
    return render(request, 'workshops/export.html', context)


@admin_required
def export_members(request):
    title = 'SCF Members'
    json_link = reverse('api:export-members', kwargs={'format': 'json'})
    yaml_link = reverse('api:export-members', kwargs={'format': 'yaml'})
    context = {
        'title': title,
        'json_link': json_link,
        'yaml_link': yaml_link,
    }
    return render(request, 'workshops/export.html', context)

#------------------------------------------------------------
#--------------------- R E P O R T S ------------------------
#------------------------------------------------------------

@admin_required
def instructors_by_date(request):
    '''Show who taught between begin_date and end_date.'''

    form = DebriefForm()
    if 'begin_date' in request.GET and 'end_date' in request.GET:
        form = DebriefForm(request.GET)

    if form.is_valid():
        start_date = form.cleaned_data['begin_date']
        end_date = form.cleaned_data['end_date']
        mode = form.cleaned_data['mode']
        rvs = ReportsViewSet()
        tasks = rvs.instructors_by_time_queryset(
            start_date, end_date,
            only_TTT=(mode == 'TTT'),
            only_non_TTT=(mode == 'nonTTT'),
        )
        emails = tasks.filter(person__may_contact=True) \
                      .exclude(person__email=None) \
                      .values_list('person__email', flat=True)
    else:
        start_date = None
        end_date = None
        tasks = None
        emails = None
        mode = 'all'

    context = {
        'title': 'List of instructors by time period',
        'form': form,
        'all_tasks': tasks,
        'emails': emails,
        'start_date': start_date,
        'end_date': end_date,
        'mode': mode,
    }
    return render(request, 'workshops/instructors_by_date.html', context)


@admin_required
def workshops_over_time(request):
    '''Export JSON of count of workshops vs. time.'''
    endpoint = '{}?{}'.format(reverse('api:reports-workshops-over-time'),
                              request.GET.urlencode())
    context = {
        'api_endpoint': endpoint,
        'filter': WorkshopsOverTimeFilter(request.GET),
        'title': 'Workshops over time',
    }
    return render(request, 'workshops/time_series.html', context)


@admin_required
def learners_over_time(request):
    '''Export JSON of count of learners vs. time.'''
    endpoint = '{}?{}'.format(reverse('api:reports-learners-over-time'),
                              request.GET.urlencode())
    context = {
        'api_endpoint': endpoint,
        'filter': LearnersOverTimeFilter(request.GET),
        'title': 'Learners over time',
    }
    return render(request, 'workshops/time_series.html', context)


@admin_required
def instructors_over_time(request):
    '''Export JSON of count of instructors vs. time.'''
    endpoint = '{}?{}'.format(reverse('api:reports-instructors-over-time'),
                              request.GET.urlencode())
    context = {
        'api_endpoint': endpoint,
        'filter': InstructorsOverTimeFilter(request.GET),
        'title': 'Instructors over time',
    }
    return render(request, 'workshops/time_series.html', context)


@admin_required
def instructor_num_taught(request):
    '''Export JSON of how often instructors have taught.'''
    context = {
        'api_endpoint': reverse('api:reports-instructor-num-taught'),
        'title': 'Frequency of Instruction',
    }
    return render(request, 'workshops/instructor_num_taught.html', context)


@admin_required
def all_activity_over_time(request):
    """Display number of workshops (of differend kinds), instructors and
    learners over some specific period of time."""

    if 'submit' in request.GET:
        form = AllActivityOverTimeForm(request.GET)

        if form.is_valid():
            data = ReportsViewSet().get_all_activity_over_time(
                start=form.cleaned_data['start'],
                end=form.cleaned_data['end'],
            )
        else:
            data = None
    else:
        form = AllActivityOverTimeForm(initial={
            'start': datetime.date.today() - datetime.timedelta(days=365),
            'end': datetime.date.today(),
        })
        data = None

    context = {
        'title': 'All activity over time',
        'form': form,
        'data': data,
    }
    return render(request, 'workshops/all_activity_over_time.html', context)


@admin_required
def membership_trainings_stats(request):
    """Display basic statistics for memberships and instructor trainings."""
    today = datetime.date.today()
    data = (
        Membership.objects
            # .filter(agreement_end__gte=today, agreement_start__lte=today)
            .select_related('organization')
            .prefetch_related('task_set')
            .annotate(
                instructor_training_seats_total=(
                    F('seats_instructor_training') +
                    F('additional_instructor_training_seats')
                ),
                instructor_training_seats_utilized=(
                    Count('task', filter=Q(task__role__name='learner'))
                ),
                instructor_training_seats_remaining=(
                    F('seats_instructor_training') +
                    F('additional_instructor_training_seats') -
                    Count('task', filter=Q(task__role__name='learner'))
                ),
            )
    )

    filter_ = MembershipTrainingsFilter(request.GET, data)
    paginated = get_pagination_items(request, filter_.qs)
    context = {
        'title': 'Membership trainings statistics',
        'data': paginated,
        'filter': filter_,
    }
    return render(request, 'workshops/membership_trainings_stats.html', context)


@admin_required
def workshop_issues(request):
    '''Display workshops in the database whose records need attention.'''

    events = Event.objects.active().past_events().annotate(
        num_instructors=Count(
            Case(
                When(
                    task__role__name='instructor',
                    then=Value(1)
                ),
                output_field=IntegerField()
            )
        )
    )

    no_attendance = Q(attendance=None) | Q(attendance=0)
    no_location = (Q(country=None) |
                   Q(venue=None) | Q(venue__exact='') |
                   Q(address=None) | Q(address__exact='') |
                   Q(latitude=None) | Q(longitude=None))
    bad_dates = Q(start__gt=F('end'))

    events = events.filter(
        (no_attendance & ~Q(tags__name='unresponsive')) |
        no_location |
        bad_dates |
        Q(num_instructors=0)
    ).prefetch_related('task_set', 'task_set__person')

    events = events.prefetch_related(Prefetch(
        'task_set',
        to_attr='contacts',
        queryset=Task.objects.select_related('person').filter(
            # we only want hosts, organizers and instructors
            Q(role__name='host') | Q(role__name='organizer') |
            Q(role__name='instructor')
        ).filter(person__may_contact=True)
        .exclude(Q(person__email='') | Q(person__email=None))
    ))

    assigned_to, is_admin = assignment_selection(request)

    if assigned_to == 'me':
        events = events.filter(assigned_to=request.user)

    elif assigned_to == 'noone':
        events = events.filter(assigned_to=None)

    elif assigned_to == 'all':
        # no filtering
        pass

    else:
        # no filtering
        pass

    events = events.annotate(
        missing_attendance=Case(
            When(no_attendance, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
        missing_location=Case(
            When(no_location, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
        bad_dates=Case(
            When(bad_dates, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    )

    context = {
        'title': 'Workshops with Issues',
        'events': events,
        'is_admin': is_admin,
        'assigned_to': assigned_to,
    }
    return render(request, 'workshops/workshop_issues.html', context)


@admin_required
def instructor_issues(request):
    '''Display instructors in the database who need attention.'''

    # Everyone who has a badge but needs attention.
    instructor_badges = Badge.objects.instructor_badges()
    instructors = Person.objects.filter(badges__in=instructor_badges) \
                                .filter(airport__isnull=True)

    # Everyone who's been in instructor training but doesn't yet have a badge.
    learner = Role.objects.get(name='learner')
    ttt = Tag.objects.get(name='TTT')
    stalled = Tag.objects.get(name='stalled')
    trainees = Task.objects \
        .filter(event__tags__in=[ttt], role=learner) \
        .exclude(person__badges__in=instructor_badges) \
        .order_by('person__family', 'person__personal', 'event__start') \
        .select_related('person', 'event')

    pending_instructors = trainees.exclude(event__tags=stalled)
    pending_instructors_person_ids = pending_instructors.values_list(
        'person__pk', flat=True,
    )

    stalled_instructors = trainees \
        .filter(event__tags=stalled) \
        .exclude(person__id__in=pending_instructors_person_ids)

    context = {
        'title': 'Instructors with Issues',
        'instructors': instructors,
        'pending': pending_instructors,
        'stalled': stalled_instructors,
    }
    return render(request, 'workshops/instructor_issues.html', context)


#------------------------------------------------------------


@admin_required
def object_changes(request, version_id):
    current_version = get_object_or_404(Version, pk=version_id)
    obj = current_version.object

    try:
        previous_version = Version.objects.get_for_object(obj) \
                                          .filter(pk__lt=current_version.pk)[0]
        obj_prev = previous_version.object
    except IndexError:
        # first revision for an object
        previous_version = current_version
        obj_prev = obj

    context = {
        'object_prev': obj_prev,
        'object': obj,
        'previous_version': previous_version,
        'current_version': current_version,
        'revision': current_version.revision,
        'title': str(obj),
        'verbose_name': obj._meta.verbose_name,
        'fields': [
            f for f in obj._meta.get_fields()
            if f.concrete
        ],
    }
    return render(request, 'workshops/object_diff.html', context)

# ------------------------------------------------------------


class AllEventRequests(OnlyForAdminsMixin, StateFilterMixin, AMYListView):
    context_object_name = 'requests'
    template_name = 'workshops/all_eventrequests.html'
    filter_class = EventRequestFilter
    queryset = EventRequest.objects.select_related('assigned_to')
    title = 'Workshop requests'


class EventRequestDetails(OnlyForAdminsMixin, AMYDetailView):
    queryset = EventRequest.objects.all()
    context_object_name = 'object'
    template_name = 'workshops/eventrequest.html'
    pk_url_kwarg = 'request_id'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Workshop request #{}'.format(self.get_object().pk)

        person_lookup_form = AdminLookupForm()
        if self.object.assigned_to:
            person_lookup_form = AdminLookupForm(
                initial={'person': self.object.assigned_to}
            )

        person_lookup_form.helper = BootstrapHelper(
            form_action=reverse('eventrequest_assign', args=[self.object.pk]),
            add_cancel_button=False)

        context['person_lookup_form'] = person_lookup_form
        return context


class EventRequestChange(OnlyForAdminsMixin, PermissionRequiredMixin,
                         AMYUpdateView):
    permission_required = 'workshops.change_eventrequest'
    model = EventRequest
    pk_url_kwarg = 'request_id'

    def get_form_class(self):
        if self.object.workshop_type == 'swc':
            return SWCEventRequestNoCaptchaForm
        elif self.object.workshop_type == 'dc':
            return DCEventRequestNoCaptchaForm
        else:
            return None


@admin_required
@permission_required('workshops.change_eventrequest', raise_exception=True)
def eventrequest_set_state(request, request_id, state):
    """Change state to selected."""
    correct_values = {
        'a': 'a',
        'accepted': 'a',
        'd': 'd',
        'discarded': 'd',
        'p': 'p',
        'pending': 'p',
    }
    if state not in correct_values.keys():
        raise Http404('Incorrect state value.')

    eventrequest = get_object_or_404(EventRequest, pk=request_id)
    eventrequest.state = correct_values[state]
    eventrequest.save()

    messages.success(request,
                     'Workshop request state was changed successfully.')
    return redirect(eventrequest.get_absolute_url())


@admin_required
@permission_required(['workshops.change_eventrequest', 'workshops.add_event'],
                     raise_exception=True)
def eventrequest_accept_event(request, request_id):
    """Accept event request by creating a new event."""
    eventrequest = get_object_or_404(EventRequest, state='p', pk=request_id)
    form = EventForm()

    if request.method == 'POST':
        form = EventForm(request.POST)

        if form.is_valid():
            event = form.save()

            eventrequest.state = 'a'
            eventrequest.event = event
            eventrequest.save()
            return redirect(reverse('event_details',
                                    args=[event.slug]))
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'object': eventrequest,
        'form': form,
    }
    return render(request, 'workshops/eventrequest_accept_event.html', context)


@admin_required
@permission_required(['workshops.change_eventrequest'], raise_exception=True)
def eventrequest_assign(request, request_id, person_id=None):
    """Set eventrequest.assigned_to. See `assign` docstring for more
    information."""
    event_req = get_object_or_404(EventRequest, pk=request_id)
    assign(request, event_req, person_id)
    return redirect(reverse('eventrequest_details', args=[event_req.pk]))


class AllProfileUpdateRequests(OnlyForAdminsMixin, AMYListView):
    context_object_name = 'requests'
    template_name = 'workshops/all_profileupdaterequests.html'
    title = 'Instructor profile update requests'
    queryset = ProfileUpdateRequest.objects.filter(active=True) \
                                           .order_by('-created_at')
    active_requests = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_requests'] = self.active_requests
        return context


class AllClosedProfileUpdateRequests(AllProfileUpdateRequests):
    queryset = ProfileUpdateRequest.objects.filter(active=False) \
                                           .order_by('-created_at')
    active_requests = False


@admin_required
def profileupdaterequest_details(request, request_id):
    update_request = get_object_or_404(ProfileUpdateRequest,
                                       pk=request_id)

    person_selected = False

    person = None
    form = None

    # Nested lookup.
    # First check if there's person with the same email, then maybe check if
    # there's a person with the same first and last names.
    try:
        person = Person.objects.get(email=update_request.email)
    except Person.DoesNotExist:
        try:
            person = Person.objects.get(personal=update_request.personal,
                                        family=update_request.family)
        except (Person.DoesNotExist, Person.MultipleObjectsReturned):
            # Either none or multiple people with the same first and last
            # names.
            # But the user might have submitted some person by themselves. We
            # should check that!
            try:
                form = PersonLookupForm(request.GET)
                person = Person.objects.get(pk=int(request.GET['person']))
                person_selected = True
            except KeyError:
                person = None
                # if the form wasn't submitted, initialize it without any
                # input data
                form = PersonLookupForm()
            except (ValueError, Person.DoesNotExist):
                person = None

    if person:
        # check if the person has instructor badge
        person.has_instructor_badge = Award.objects.filter(
            badge__in=Badge.objects.instructor_badges(), person=person
        ).exists()

    try:
        airport = Airport.objects.get(iata__iexact=update_request.airport_iata)
    except Airport.DoesNotExist:
        airport = None

    context = {
        'title': ('Instructor profile update request #{}'
                  .format(update_request.pk)),
        'new': update_request,
        'old': person,
        'person_form': form,
        'person_selected': person_selected,
        'airport': airport,
    }
    return render(request, 'workshops/profileupdaterequest.html', context)


class ProfileUpdateRequestFix(OnlyForAdminsMixin, PermissionRequiredMixin,
                              AMYUpdateView):
    permission_required = 'workshops.change_profileupdaterequest'
    model = ProfileUpdateRequest
    form_class = ProfileUpdateRequestFormNoCaptcha
    pk_url_kwarg = 'request_id'


@admin_required
@permission_required('workshops.change_profileupdaterequest',
                     raise_exception=True)
def profileupdaterequest_discard(request, request_id):
    """Discard ProfileUpdateRequest, ie. set it to inactive."""
    profileupdate = get_object_or_404(ProfileUpdateRequest, active=True,
                                      pk=request_id)
    profileupdate.active = False
    profileupdate.save()

    messages.success(request,
                     'Profile update request was discarded successfully.')
    return redirect(reverse('all_profileupdaterequests'))


@admin_required
@permission_required('workshops.change_profileupdaterequest',
                     raise_exception=True)
def profileupdaterequest_accept(request, request_id, person_id=None):
    """
    Accept the profile update by rewriting values to selected user's profile.

    IMPORTANT: we do not rewrite all of the data users input (like
    other gender, or other lessons).  All of it is still in
    the database model ProfileUpdateRequest, but does not get written to the
    Person model object.
    """
    profileupdate = get_object_or_404(ProfileUpdateRequest, active=True,
                                      pk=request_id)
    airport = get_object_or_404(Airport, iata__iexact=profileupdate.airport_iata)

    if person_id is None:
        person = Person()
        # since required perms change depending on `person_id`, we have to
        # check the perms programmatically; here user is required
        # `workshops.add_person` in order to add a new person
        if not request.user.has_perm('workshops.add_person'):
            raise PermissionDenied
    else:
        person = get_object_or_404(Person, pk=person_id)
        person_name = str(person)
        # since required perms change depending on `person_id`, we have to
        # check the perms programmatically; here user is required
        # `workshops.change_person` in order to set existing person's fields
        if not request.user.has_perm('workshops.change_person'):
            raise PermissionDenied

    person.personal = profileupdate.personal
    person.middle = profileupdate.middle
    person.family = profileupdate.family
    person.email = profileupdate.email
    person.affiliation = profileupdate.affiliation
    person.country = profileupdate.country
    person.airport = airport
    person.github = profileupdate.github
    person.twitter = profileupdate.twitter
    person.url = profileupdate.website
    # if occupation is "Other", simply save the `occupation_other` field,
    # otherwise get full display of occupation (since it's a choice field)
    if profileupdate.occupation == '':
        person.occupation = profileupdate.occupation_other
    else:
        person.occupation = profileupdate.get_occupation_display()
    person.orcid = profileupdate.orcid
    person.gender = profileupdate.gender
    person.user_notes = profileupdate.notes

    with transaction.atomic():
        # we need person to exist in the database in order to set domains and
        # lessons
        if not person.id:
            try:
                person.username = create_username(person.personal,
                                                  person.family)
                person.save()
            except IntegrityError:
                messages.error(
                    request,
                    'Cannot update profile: some database constraints weren\'t'
                    ' fulfilled. Make sure that user name, GitHub user name,'
                    ' Twitter user name, or email address are unique.'
                )
                return redirect(profileupdate.get_absolute_url())

        person.domains.set(list(profileupdate.domains.all()))
        person.languages.set(profileupdate.languages.all())

        try:
            person.save()
        except IntegrityError:
            messages.error(
                request,
                'Cannot update profile: some database constraints weren\'t'
                'fulfilled. Make sure that user name, GitHub user name,'
                'Twitter user name, or email address are unique.'
            )
            return redirect(profileupdate.get_absolute_url())

        # Since Person.lessons uses a intermediate model Qualification, we ought to
        # operate on Qualification objects instead of using Person.lessons as a
        # list.

        # erase old lessons
        Qualification.objects.filter(person=person).delete()
        # add new
        Qualification.objects.bulk_create([
            Qualification(person=person, lesson=L)
            for L in profileupdate.lessons.all()
        ])

        profileupdate.active = False
        profileupdate.save()

    if person_id is None:
        messages.success(request,
                         'New person was added successfully.')
    else:
        messages.success(request,
                         '{} was updated successfully.'.format(person_name))

    return redirect(person.get_absolute_url())


class AllEventSubmissions(OnlyForAdminsMixin, StateFilterMixin, AMYListView):
    context_object_name = 'submissions'
    template_name = 'workshops/all_eventsubmissions.html'
    filter_class = EventSubmissionFilter
    queryset = EventSubmissionModel.objects.all()
    title = 'Workshop submissions'


class EventSubmissionDetails(OnlyForAdminsMixin, AMYDetailView):
    context_object_name = 'object'
    template_name = 'workshops/eventsubmission.html'
    queryset = EventSubmissionModel.objects.all()
    pk_url_kwarg = 'submission_id'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Workshop submission #{}'.format(
            self.get_object().pk)

        person_lookup_form = AdminLookupForm()
        if self.object.assigned_to:
            person_lookup_form = AdminLookupForm(
                initial={'person': self.object.assigned_to}
            )

        person_lookup_form.helper = BootstrapHelper(
            form_action=reverse('eventsubmission_assign',
                                args=[self.object.pk]))

        context['person_lookup_form'] = person_lookup_form
        return context


class EventSubmissionChange(OnlyForAdminsMixin, PermissionRequiredMixin,
                            AMYUpdateView):
    permission_required = 'workshops.change_eventsubmission'
    model = EventSubmissionModel
    form_class = EventSubmitFormNoCaptcha
    pk_url_kwarg = 'submission_id'


@admin_required
@permission_required(['workshops.change_eventsubmission',
                      'workshops.add_event'], raise_exception=True)
def eventsubmission_accept_event(request, submission_id):
    """Accept event submission by creating a new event."""
    submission = get_object_or_404(EventSubmissionModel, state='p',
                                   pk=submission_id)
    form = EventForm()

    if request.method == 'POST':
        form = EventForm(request.POST)

        if form.is_valid():
            event = form.save()

            submission.state = 'a'
            submission.event = event
            submission.save()
            return redirect(reverse('event_details',
                                    args=[event.slug]))
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'object': submission,
        'form': form,
        'title': None,
    }
    return render(request, 'workshops/eventsubmission_accept_event.html',
                  context)


@admin_required
@permission_required('workshops.change_eventsubmission', raise_exception=True)
def eventsubmission_set_state(request, submission_id, state):
    """Change state to selected."""
    correct_values = {
        'a': 'a',
        'accepted': 'a',
        'd': 'd',
        'discarded': 'd',
        'p': 'p',
        'pending': 'p',
    }
    if state not in correct_values.keys():
        raise Http404('Incorrect state value.')

    submission = get_object_or_404(EventSubmissionModel, pk=submission_id)
    submission.state = correct_values[state]
    submission.save()

    messages.success(request,
                     'Workshop submission state was changed successfully.')
    return redirect(submission.get_absolute_url())


@admin_required
@permission_required(['workshops.change_eventsubmission'], raise_exception=True)
def eventsubmission_assign(request, submission_id, person_id=None):
    """Set eventsubmission.assigned_to. See `assign` docstring for more
    information."""
    submission = get_object_or_404(EventSubmissionModel, pk=submission_id)
    assign(request, submission, person_id)
    return redirect(submission.get_absolute_url())


class AllDCSelfOrganizedEventRequests(OnlyForAdminsMixin, StateFilterMixin,
                                      AMYListView):
    context_object_name = 'requests'
    template_name = 'workshops/all_dcselforganizedeventrequests.html'
    filter_class = DCSelfOrganizedEventRequestFilter
    queryset = DCSelfOrganizedEventRequestModel.objects.all()
    title = 'Data Carpentry self-organized workshop requests'


class DCSelfOrganizedEventRequestDetails(OnlyForAdminsMixin, AMYDetailView):
    context_object_name = 'object'
    template_name = 'workshops/dcselforganizedeventrequest.html'
    queryset = DCSelfOrganizedEventRequestModel.objects.all()
    pk_url_kwarg = 'request_id'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'DC self-organized workshop request #{}'.format(
            self.get_object().pk)

        person_lookup_form = AdminLookupForm()
        if self.object.assigned_to:
            person_lookup_form = AdminLookupForm(
                initial={'person': self.object.assigned_to}
            )

        person_lookup_form.helper = BootstrapHelper(
            form_action=reverse('dcselforganizedeventrequest_assign',
                                args=[self.object.pk]))

        context['person_lookup_form'] = person_lookup_form
        return context


class DCSelfOrganizedEventRequestChange(OnlyForAdminsMixin,
                                        PermissionRequiredMixin,
                                        AMYUpdateView):
    permission_required = 'workshops.change_dcselforganizedeventrequest'
    model = DCSelfOrganizedEventRequestModel
    form_class = DCSelfOrganizedEventRequestFormNoCaptcha
    pk_url_kwarg = 'request_id'


@admin_required
@permission_required('workshops.change_dcselforganizedeventrequest',
                     raise_exception=True)
def dcselforganizedeventrequest_set_state(request, request_id, state):
    """Change state to selected."""
    correct_values = {
        'a': 'a',
        'accepted': 'a',
        'd': 'd',
        'discarded': 'd',
        'p': 'p',
        'pending': 'p',
    }
    if state not in correct_values.keys():
        raise Http404('Incorrect state value.')

    event_req = get_object_or_404(DCSelfOrganizedEventRequestModel,
                                  pk=request_id)
    event_req.state = correct_values[state]
    event_req.save()

    messages.success(request,
                     'DC self-organized workshop request state was changed'
                     ' successfully.')
    return redirect(event_req.get_absolute_url())


@admin_required
@permission_required(['workshops.change_dcselforganizedeventrequest',
                      'workshops.add_event'],
                     raise_exception=True)
def dcselforganizedeventrequest_accept_event(request, request_id):
    """Accept DC self-org. event request by creating a new event."""
    event_req = get_object_or_404(DCSelfOrganizedEventRequestModel, state='p',
                                  pk=request_id)
    form = EventForm()

    if request.method == 'POST':
        form = EventForm(request.POST)

        if form.is_valid():
            event = form.save()

            event_req.state = 'a'
            event_req.event = event
            event_req.save()
            return redirect(reverse('event_details',
                                    args=[event.slug]))
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'object': event_req,
        'form': form,
    }
    return render(request,
                  'workshops/dcselforganizedeventrequest_accept_event.html',
                  context)


@admin_required
@permission_required(['workshops.change_dcselforganizedeventrequest'],
                     raise_exception=True)
def dcselforganizedeventrequest_assign(request, request_id, person_id=None):
    """Set eventrequest.assigned_to. See `assign` docstring for more
    information."""
    event_req = get_object_or_404(DCSelfOrganizedEventRequestModel,
                                  pk=request_id)
    assign(request, event_req, person_id)
    return redirect(reverse('dcselforganizedeventrequest_details',
                            args=[event_req.pk]))

#------------------------------------------------------------


@admin_required
@permission_required('workshops.add_todoitem', raise_exception=True)
def todos_add(request, slug):
    """Add a standard TodoItems for a specific event."""
    try:
        event = Event.objects.get(slug=slug)
    except Event.DoesNotExist:
        raise Http404('Event matching query does not exist.')

    dt = datetime.datetime
    timedelta = datetime.timedelta

    initial = []
    base = dt.now()

    if not event.start or not event.end:
        initial = [
            {
                'title': 'Set date with host',
                'due': dt.now() + timedelta(days=30),
                'event': event,
            },
        ]

    formset = TodoFormSet(queryset=TodoItem.objects.none(), initial=initial + [
        {
            'title': 'Set up a workshop website',
            'due': base + timedelta(days=7),
            'event': event,
        },
        {
            'title': 'Find instructor #1',
            'due': base + timedelta(days=14),
            'event': event,
        },
        {
            'title': 'Find instructor #2',
            'due': base + timedelta(days=14),
            'event': event,
        },
        {
            'title': 'Follow up that instructors have booked travel',
            'due': base + timedelta(days=21),
            'event': event,
        },
        {
            'title': 'Set up pre-workshop survey',
            'due': event.start - timedelta(days=7) if event.start else '',
            'event': event,
        },
        {
            'title': 'Make sure instructors are set with materials',
            'due': event.start - timedelta(days=1) if event.start else '',
            'event': event,
        },
        {
            'title': 'Submit invoice',
            'due': event.end + timedelta(days=2) if event.end else '',
            'event': event,
        },
        {
            'title': 'Make sure instructors are reimbursed',
            'due': event.end + timedelta(days=7) if event.end else '',
            'event': event,
        },
        {
            'title': 'Get attendee list',
            'due': event.end + timedelta(days=7) if event.end else '',
            'event': event,
        },
    ])

    if request.method == 'POST':
        formset = TodoFormSet(request.POST)
        if formset.is_valid():
            formset.save()
            messages.success(request, 'Successfully added a bunch of TODOs.',
                             extra_tags='todos')
            return redirect(reverse(event_details, args=(event.slug, )))
        else:
            messages.error(request, 'Fix errors below.')

    formset.helper = bootstrap_helper_inline_formsets

    context = {
        'title': 'Add standard TODOs to the event',
        'formset': formset,
        'event': event,
    }
    return render(request, 'workshops/todos_add.html', context)


@admin_required
@permission_required('workshops.change_todoitem', raise_exception=True)
def todo_mark_completed(request, todo_id):
    todo = get_object_or_404(TodoItem, pk=todo_id)

    todo.completed = True
    todo.save()

    return HttpResponse()


@admin_required
@permission_required('workshops.change_todoitem', raise_exception=True)
def todo_mark_incompleted(request, todo_id):
    todo = get_object_or_404(TodoItem, pk=todo_id)

    todo.completed = False
    todo.save()

    return HttpResponse()


class TodoItemUpdate(OnlyForAdminsMixin, PermissionRequiredMixin,
                     AMYUpdateView):
    permission_required = 'workshops.change_todoitem'
    model = TodoItem
    form_class = SimpleTodoForm
    pk_url_kwarg = 'todo_id'

    def get_success_url(self):
        return reverse('event_details', args=[self.object.event.slug])

    def form_valid(self, form):
        """Overwrite default way of showing the success message, because we
        need to add extra tags to it)."""
        self.object = form.save()

        # Important: we need to use ModelFormMixin.form_valid() here!
        # But by doing so we omit SuccessMessageMixin completely, so we need to
        # simulate it.  The code below is almost identical to
        # SuccessMessageMixin.form_valid().
        response = super(ModelFormMixin, self).form_valid(form)
        success_message = self.get_success_message(form.cleaned_data)
        if success_message:
            messages.success(self.request, success_message, extra_tags='todos')
        return response


class TodoDelete(OnlyForAdminsMixin, PermissionRequiredMixin,
                 AMYDeleteView):
    model = TodoItem
    permission_required = 'workshops.delete_todoitem'
    pk_url_kwarg = 'todo_id'

    def get_success_url(self):
        return reverse('event_details', args=[self.get_object().event.slug]) + '#todos'

# ------------------------------------------------------------

@admin_required
def duplicate_persons(request):
    """Find possible duplicates amongst persons.

    Criteria for persons:
    * switched personal/family names
    * same name on different people."""
    names_normal = set(Person.objects.all().values_list('personal', 'family'))
    names_switched = set(Person.objects.all().values_list('family',
                                                          'personal'))
    names = names_normal & names_switched  # intersection

    switched_criteria = Q(id=0)
    # empty query results if names is empty
    for personal, family in names:
        # get people who appear in `names`
        switched_criteria |= (Q(personal=personal) & Q(family=family))

    switched_persons = Person.objects.filter(switched_criteria) \
                                     .order_by('email')

    duplicate_names = Person.objects.values('personal', 'family') \
                                    .order_by() \
                                    .annotate(count_id=Count('id')) \
                                    .filter(count_id__gt=1)

    duplicate_criteria = Q(id=0)
    for name in duplicate_names:
        # get people who appear in `names`
        duplicate_criteria |= (Q(personal=name['personal']) &
                               Q(family=name['family']))
    duplicate_persons = Person.objects.filter(duplicate_criteria) \
                                      .order_by('family', 'personal', 'email')

    context = {
        'title': 'Possible duplicate persons',
        'switched_persons': switched_persons,
        'duplicate_persons': duplicate_persons,
    }

    return render(request, 'workshops/duplicate_persons.html', context)


@admin_required
def duplicate_training_requests(request):
    """Find possible duplicates amongst training requests.

    Criteria:
    * the same name
    * the same email.
    """
    names = (
        TrainingRequest.objects
            .values('personal', 'family')
            .order_by('family', 'personal')
            .annotate(count_id=Count('id'))
            .filter(count_id__gt=1)
    )
    duplicate_names_criteria = Q(id=0)
    for name in names:
        duplicate_names_criteria |= (Q(personal=name['personal']) &
                                     Q(family=name['family']))

    emails = (
        TrainingRequest.objects
            .values_list('email', flat=True)
            .order_by('family', 'personal')
            .annotate(count_id=Count('id'))
            .filter(count_id__gt=1)
    )
    duplicate_emails_criteria = Q(id=0)
    for email in emails:
        duplicate_emails_criteria |= Q(email=email)

    duplicate_names = TrainingRequest.objects.filter(duplicate_names_criteria).order_by('family', 'personal')
    duplicate_emails = TrainingRequest.objects.filter(duplicate_emails_criteria).order_by('email')

    context = {
        'title': 'Possible duplicate training requests',
        'duplicate_names': duplicate_names,
        'duplicate_emails': duplicate_emails,
    }

    return render(request, 'workshops/duplicate_training_requests.html',
                  context)


@admin_required
def all_trainingrequests(request):
    filter = TrainingRequestFilter(
        request.GET,
        queryset=TrainingRequest.objects.all().prefetch_related(
            Prefetch('person__task_set',
                     to_attr='training_tasks',
                     queryset=Task.objects
                        .filter(role__name='learner', event__tags__name='TTT')
                        .select_related('event')
                     ),
        )
    )

    emails = filter.qs.values_list('email', flat=True)
    requests = get_pagination_items(request, filter.qs)

    if request.method == 'POST' and 'match' in request.POST:
        # Bulk match people associated with selected TrainingRequests to
        # trainings.
        form = BulkChangeTrainingRequestForm()
        match_form = BulkMatchTrainingRequestForm(request.POST)

        if match_form.is_valid():
            member_site = match_form.cleaned_data['seat_membership']

            # Perform bulk match
            for r in match_form.cleaned_data['requests']:
                # automatically accept this request
                r.state = 'a'
                r.save()

                # assign to an event
                Task.objects.get_or_create(
                    person=r.person,
                    role=Role.objects.get(name='learner'),
                    event=match_form.cleaned_data['event'],
                    seat_membership=member_site)

            requests_count = len(match_form.cleaned_data['requests'])
            today = datetime.date.today()

            if member_site:
                if member_site.seats_instructor_training_remaining - requests_count <= 0:
                    messages.warning(
                        request,
                        'Membership "{}" is using more training seats than it\'s '
                        'been allowed.'.format(str(member_site)),
                    )

                # check if membership is active
                if not (member_site.agreement_start <= today <= member_site.agreement_end):
                    messages.warning(
                        request,
                        'Membership "{}" is not active.'.format(str(member_site))
                    )

            messages.success(request, 'Successfully accepted and matched '
                                      'selected people to training.')

            # Raw uri contains GET parameters from django filters. We use it
            # to preserve filter settings.
            return redirect(request.get_raw_uri())

    elif request.method == 'POST' and 'discard' in request.POST:
        # Bulk discard selected TrainingRequests.
        form = BulkChangeTrainingRequestForm(request.POST)
        match_form = BulkMatchTrainingRequestForm()

        if form.is_valid():
            # Perform bulk discard
            for r in form.cleaned_data['requests']:
                r.state = 'd'
                r.save()

            messages.success(request, 'Successfully discarded selected '
                                      'requests.')

            return redirect(request.get_raw_uri())

    elif request.method == 'POST' and 'accept' in request.POST:
        # Bulk discard selected TrainingRequests.
        form = BulkChangeTrainingRequestForm(request.POST)
        match_form = BulkMatchTrainingRequestForm()

        if form.is_valid():
            # Perform bulk discard
            for r in form.cleaned_data['requests']:
                r.state = 'a'
                r.save()

            messages.success(request, 'Successfully accepted selected '
                                      'requests.')

            return redirect(request.get_raw_uri())

    elif request.method == 'POST' and 'unmatch' in request.POST:
        # Bulk unmatch people associated with selected TrainingRequests from
        # trainings.
        form = BulkChangeTrainingRequestForm(request.POST)
        match_form = BulkMatchTrainingRequestForm()

        form.check_person_matched = True
        if form.is_valid():
            # Perform bulk unmatch
            for r in form.cleaned_data['requests']:
                r.person.get_training_tasks().delete()

            messages.success(request, 'Successfully unmatched selected '
                                      'people from trainings.')

            return redirect(request.get_raw_uri())

    else:  # GET request
        form = BulkChangeTrainingRequestForm()
        match_form = BulkMatchTrainingRequestForm()

    context = {
        'title': 'Training Requests',
        'requests': requests,
        'filter': filter,
        'form': form,
        'match_form': match_form,
        'emails': emails,
    }

    return render(request, 'workshops/all_trainingrequests.html', context)


def _match_training_request_to_person(request, training_request, create=False,
                                      person=None):
    if create:
        try:
            training_request.person = Person.objects.create_user(
                username=create_username(training_request.personal,
                                         training_request.family),
                personal=training_request.personal,
                family=training_request.family,
                email=training_request.email,
            )
        except IntegrityError as e:
            # email address is not unique
            messages.error(request, 'Could not create a new person, because '
                                    'there already exists a person with '
                                    'exact email address.')
            return False

    else:
        training_request.person = person

    # as per #1270:
    # https://github.com/swcarpentry/amy/issues/1270#issuecomment-407515948
    # let's rewrite everything that's possible to rewrite
    try:
        training_request.person.personal = training_request.personal
        training_request.person.middle = training_request.middle
        training_request.person.family = training_request.family
        training_request.person.email = training_request.email
        training_request.person.country = training_request.country
        training_request.person.github = training_request.github
        training_request.person.affiliation = training_request.affiliation
        training_request.person.domains.set(training_request.domains.all())
        training_request.person.occupation = (
            training_request.get_occupation_display()
            if training_request.occupation else
            training_request.occupation_other)
        training_request.person.data_privacy_agreement = \
            training_request.data_privacy_agreement

        training_request.person.may_contact = True
        training_request.person.is_active = True

        # merge notes
        training_request.person.notes = (
            training_request.person.notes +"\n\nNotes from training request:\n"
            + training_request.notes)

        training_request.person.save()
        training_request.person.synchronize_usersocialauth()
        training_request.save()

        messages.success(request, 'Request matched with the person.')

        return True
    except IntegrityError as e:
        # email or github not unique
        messages.error(request, "It was impossible to update related person's "
                                "data. Probably email address or GitHub "
                                "handle used in the training request are not "
                                " unique amongst person entries.")
        return False


@admin_required
def trainingrequest_details(request, pk):
    req = get_object_or_404(TrainingRequest, pk=int(pk))

    if request.method == 'POST':
        form = MatchTrainingRequestForm(request.POST)

        if form.is_valid():
            create = (form.action == "create")
            person = form.cleaned_data['person']
            ok = _match_training_request_to_person(request,
                                                   training_request=req,
                                                   create=create,
                                                   person=person)
            if ok:
                return redirect_with_next_support(
                    request, 'trainingrequest_details', req.pk)

    else:  # GET request
        # Provide initial value for form.person
        if req.person is not None:
            person = req.person
        else:
            # No person is matched to the TrainingRequest yet. Suggest a
            # person from existing records.
            person = Person.objects.filter(Q(email__iexact=req.email) |
                                           Q(personal__iexact=req.personal,
                                             middle__iexact=req.middle,
                                             family__iexact=req.family)) \
                                   .first()  # may return None
        form = MatchTrainingRequestForm(initial={'person': person})

    context = {
        'title': 'Training request #{}'.format(req.pk),
        'req': req,
        'form': form,
    }
    return render(request, 'workshops/trainingrequest.html', context)


@admin_required
@permission_required(['workshops.delete_trainingrequest',
                      'workshops.change_trainingrequest'],
                     raise_exception=True)
def trainingrequests_merge(request):
    """Display two training requests side by side on GET and merge them on
    POST.

    If no requests are supplied via GET params, display event selection form."""
    obj_a_pk = request.GET.get('trainingrequest_a')
    obj_b_pk = request.GET.get('trainingrequest_b')

    if not obj_a_pk or not obj_b_pk:
        context = {
            'title': 'Merge Training Requests',
            'form': TrainingRequestsSelectionForm(),
        }
        return render(request, 'workshops/generic_form.html', context)

    obj_a = get_object_or_404(TrainingRequest, pk=obj_a_pk)
    obj_b = get_object_or_404(TrainingRequest, pk=obj_b_pk)

    form = TrainingRequestsMergeForm(initial=dict(trainingrequest_a=obj_a,
                                                  trainingrequest_b=obj_b))

    if request.method == "POST":
        form = TrainingRequestsMergeForm(request.POST)

        if form.is_valid():
            # merging in process
            data = form.cleaned_data

            obj_a = data['trainingrequest_a']
            obj_b = data['trainingrequest_b']

            # `base_obj` stays in the database after merge
            # `merging_obj` will be removed from DB after merge
            if data['id'] == 'obj_a':
                base_obj = obj_a
                merging_obj = obj_b
                base_a = True
            else:
                base_obj = obj_b
                merging_obj = obj_a
                base_a = False

            # non-M2M-relationships:
            easy = (
                'state', 'person', 'group_name', 'personal', 'middle',
                'family', 'email', 'github', 'occupation', 'occupation_other',
                'affiliation', 'location', 'country', 'underresourced',
                'domains_other', 'underrepresented',
                'nonprofit_teaching_experience',
                'previous_training', 'previous_training_other',
                'previous_training_explanation', 'previous_experience',
                'previous_experience_other', 'previous_experience_explanation',
                'programming_language_usage_frequency',
                'teaching_frequency_expectation',
                'teaching_frequency_expectation_other',
                'max_travelling_frequency', 'max_travelling_frequency_other',
                'reason', 'comment', 'training_completion_agreement',
                'workshop_teaching_agreement',
                'data_privacy_agreement', 'code_of_conduct_agreement',
                'created_at', 'last_updated_at',
                'notes',
            )
            # M2M relationships
            difficult = (
                'domains', 'previous_involvement',
            )

            try:
                _, integrity_errors = merge_objects(obj_a, obj_b, easy,
                                                    difficult, choices=data,
                                                    base_a=base_a)

                if integrity_errors:
                    msg = ('There were integrity errors when merging related '
                           'objects:\n' '\n'.join(integrity_errors))
                    messages.warning(request, msg)

            except ProtectedError as e:
                return failed_to_delete(request, object=merging_obj,
                                        protected_objects=e.protected_objects)

            else:
                return redirect(base_obj.get_absolute_url())
        else:
            messages.error(request, 'Fix errors in the form.')

    context = {
        'title': 'Merge two training requets',
        'obj_a': obj_a,
        'obj_b': obj_b,
        'form': form,
    }
    return render(request, 'workshops/trainingrequests_merge.html', context)


# ------------------------------------------------------------
# Views for trainees


@login_required
def trainee_dashboard(request):
    # Workshops person taught at
    workshops = request.user.task_set.all()

    context = {
        'title': 'Your profile',
        'workshops': workshops,
    }
    return render(request, 'workshops/trainee_dashboard.html', context)


@login_required
def autoupdate_profile(request):
    person = request.user
    form = AutoUpdateProfileForm(instance=person)

    if request.method == 'POST':
        form = AutoUpdateProfileForm(request.POST, instance=person)

        if form.is_valid() and form.instance == person:
            # save lessons
            person.lessons.clear()
            for lesson in form.cleaned_data['lessons']:
                q = Qualification(lesson=lesson, person=person)
                q.save()

            # don't save related lessons
            del form.cleaned_data['lessons']

            person = form.save()

            messages.success(request, 'Your profile was updated.')

            return redirect(reverse('trainee-dashboard'))
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'title': 'Update Your Profile',
        'form': form,
    }
    return render(request, 'workshops/autoupdate_profile.html', context)


@login_required
def training_progress(request):
    swc_form = SendHomeworkForm(submit_name='swc-submit')
    dc_form = SendHomeworkForm(submit_name='dc-submit')

    # Add information about instructor training progress to request.user.
    request.user = Person.objects.annotate_with_instructor_eligibility() \
                                 .get(pk=request.user.pk)

    progresses = request.user.trainingprogress_set.filter(discarded=False)
    last_swc_homework = progresses.filter(
        requirement__name='SWC Homework').order_by('-created_at').first()
    request.user.swc_homework_in_evaluation = (
        last_swc_homework is not None and last_swc_homework.state == 'n')
    last_dc_homework = progresses.filter(
        requirement__name='DC Homework').order_by('-created_at').first()
    request.user.dc_homework_in_evaluation = (
        last_dc_homework is not None and last_dc_homework.state == 'n')

    # Add information about awarded instructor badges to request.user.
    request.user.is_swc_instructor = request.user.award_set.filter(
        badge__name='swc-instructor').exists()
    request.user.is_dc_instructor = request.user.award_set.filter(
        badge__name='dc-instructor').exists()

    if request.method == 'POST' and 'swc-submit' in request.POST:
        requirement = TrainingRequirement.objects.get(name='SWC Homework')
        progress = TrainingProgress(trainee=request.user,
                                    state='n',  # not-evaluated yet
                                    requirement=requirement)
        swc_form = SendHomeworkForm(data=request.POST, instance=progress,
                                    submit_name='swc-submit')
        dc_form = SendHomeworkForm(submit_name='dc-submit')

        if swc_form.is_valid():
            swc_form.save()
            messages.success(request, 'Your homework submission will be '
                                      'evaluated soon.')
            return redirect(reverse('training-progress'))

    elif request.method == 'POST' and 'dc-submit' in request.POST:
        requirement = TrainingRequirement.objects.get(name='DC Homework')
        progress = TrainingProgress(trainee=request.user,
                                    state='n',  # not-evaluated yet
                                    requirement=requirement)
        swc_form = SendHomeworkForm(submit_name='swc-submit')
        dc_form = SendHomeworkForm(data=request.POST, instance=progress,
                                    submit_name='dc-submit')

        if dc_form.is_valid():
            dc_form.save()
            messages.success(request, 'Your homework submission will be '
                                      'evaluated soon.')
            return redirect(reverse('training-progress'))

    else:  # GET request
        pass

    context = {
        'title': 'Your training progress',
        'swc_form': swc_form,
        'dc_form': dc_form,
    }
    return render(request, 'workshops/training_progress.html', context)


# ------------------------------------------------------------
# Instructor Training related views


class TrainingRequestUpdate(RedirectSupportMixin,
                            OnlyForAdminsMixin,
                            AMYUpdateView):
    model = TrainingRequest
    form_class = TrainingRequestUpdateForm


class TrainingProgressCreate(RedirectSupportMixin,
                             PrepopulationSupportMixin,
                             OnlyForAdminsMixin,
                             AMYCreateView):
    model = TrainingProgress
    form_class = TrainingProgressForm
    populate_fields = ['trainee']

    def get_initial(self):
        initial = super().get_initial()
        initial['evaluated_by'] = self.request.user
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'].helper = context['form'].create_helper
        return context


class TrainingProgressUpdate(RedirectSupportMixin, OnlyForAdminsMixin,
                             AMYUpdateView):
    model = TrainingProgress
    form_class = TrainingProgressForm
    template_name = 'workshops/trainingprogress_form.html'


class TrainingProgressDelete(RedirectSupportMixin, OnlyForAdminsMixin,
                             AMYDeleteView):
    model = TrainingProgress
    success_url = reverse_lazy('all_trainees')


@admin_required
def all_trainees(request):
    filter = TraineeFilter(
        request.GET,
        queryset=Person.objects
            .annotate_with_instructor_eligibility()
            .defer('notes')  # notes are too large, so we defer them
            .prefetch_related(
                Prefetch('task_set',
                         to_attr='training_tasks',
                         queryset=Task.objects.filter(role__name='learner',
                                                      event__tags__name='TTT')),
                'training_tasks__event',
                'trainingrequest_set',
                'trainingprogress_set',
                'trainingprogress_set__requirement',
                'trainingprogress_set__evaluated_by',
            )
            .annotate(
                is_swc_instructor=Sum(Case(When(badges__name='swc-instructor',
                                                then=1),
                                           default=0,
                                           output_field=IntegerField())),
                is_dc_instructor=Sum(Case(When(badges__name='dc-instructor',
                                               then=1),
                                          default=0,
                                          output_field=IntegerField())),
        )
    )
    trainees = get_pagination_items(request, filter.qs)

    if request.method == 'POST' and 'discard' in request.POST:
        # Bulk discard progress of selected trainees
        form = BulkAddTrainingProgressForm()
        discard_form = BulkDiscardProgressesForm(request.POST)
        if discard_form.is_valid():
            for trainee in discard_form.cleaned_data['trainees']:
                TrainingProgress.objects.filter(trainee=trainee)\
                                        .update(discarded=True)
            messages.success(request, 'Successfully discarded progress of '
                                      'all selected trainees.')

            # Raw uri contains GET parameters from django filters. We use it
            # to preserve filter settings.
            return redirect(request.get_raw_uri())

    elif request.method == 'POST' and 'submit' in request.POST:
        # Bulk add progress to selected trainees
        instance = TrainingProgress(evaluated_by=request.user)
        form = BulkAddTrainingProgressForm(request.POST, instance=instance)
        discard_form = BulkDiscardProgressesForm()
        if form.is_valid():
            for trainee in form.cleaned_data['trainees']:
                TrainingProgress.objects.create(
                    trainee=trainee,
                    evaluated_by=request.user,
                    requirement=form.cleaned_data['requirement'],
                    state=form.cleaned_data['state'],
                    discarded=False,
                    event=form.cleaned_data['event'],
                    url=form.cleaned_data['url'],
                    notes=form.cleaned_data['notes'],
                )
            messages.success(request, 'Successfully changed progress of '
                                      'all selected trainees.')

            return redirect(request.get_raw_uri())

    else:  # GET request
        # If the user filters by training, we want to set initial values for
        # "requirement" and "training" fields.
        training_id = request.GET.get('training', None) or None
        try:
            initial = {
                'event': Event.objects.get(pk=training_id),
                'requirement': TrainingRequirement.objects.get(name='Training')
            }
        except Event.DoesNotExist:  # or there is no `training` GET parameter
            initial = None

        form = BulkAddTrainingProgressForm(initial=initial)
        discard_form = BulkDiscardProgressesForm()

    context = {'title': 'Trainees',
               'all_trainees': trainees,
               'swc': Badge.objects.get(name='swc-instructor'),
               'dc': Badge.objects.get(name='dc-instructor'),
               'filter': filter,
               'form': form,
               'discard_form': discard_form}
    return render(request, 'workshops/all_trainees.html', context)


# ------------------------------------------------------------
# "Action required" views

@login_required
def action_required_privacy(request):
    person = request.user

    # disable the view for users who already agreed
    if person.data_privacy_agreement:
        return redirect(reverse('dispatch'))

    form = ActionRequiredPrivacyForm(instance=person)

    if request.method == 'POST':
        form = ActionRequiredPrivacyForm(request.POST, instance=person)

        if form.is_valid() and form.instance == person:
            person = form.save()
            messages.success(request, 'Agreement successfully saved.')

            return redirect(reverse('dispatch'))
        else:
            messages.error(request, 'Fix errors below.')

    context = {
        'title': 'Action required: privacy policy agreement',
        'form': form,
    }
    return render(request, 'workshops/action_required_privacy.html', context)
