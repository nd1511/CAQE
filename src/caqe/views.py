"""
URL route handlers
"""

import json
import logging
import random
import urlparse
import datetime
import functools

from flask import request, render_template, flash, redirect, session, make_response, send_from_directory, \
    safe_join

import experiment

from caqe import app
from caqe import db
from models import Participant, Trial, Condition
from settings import *
from caqe import utilities

logger = logging.getLogger(__name__)


def nocache(view):
    """
    No cache decorator. Puts no cache directives in header to avoid caching of endpoint.

    Parameters
    ----------
    view : flask view function
    """

    @functools.wraps(view)
    def no_cache(*args, **kwargs):
        response = make_response(view(*args, **kwargs))
        response.headers['Last-Modified'] = datetime.datetime.now()
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
        return response

    return functools.update_wrapper(no_cache, view)


def strip_query_from_url(url):
    """
    Return the a URL without the query, which may be simply used for cache busting.

    Returns
    -------
    stripped_url : str
        URL stripped of query strings
    """
    # strip query portion (since its a hash for cache busting of static urls)
    split_url = list(urlparse.urlsplit(url))
    split_url[3] = ''
    stripped_url = urlparse.urlunsplit(split_url)
    return stripped_url


def get_current_participant(current_session, allow_none=False):
    """
    Get the participant based on the `participant_id` in the current session.

    Parameters
    ----------
    current_session : flask.Session
    allow_none : bool, optional
        If `allow_none`==False, then if `participant_id` is not defined or the id is invalid, then raise an exception,
        otherwise return None. Default is False.

    Returns
    -------
    participant : caqe.models.Participant
    """
    participant_id = current_session.get('participant_id', None)
    if participant_id is None:
        if not allow_none:
            # if this happens, either there is a bug in the software, or we are having trouble writing and retrieving
            # to the session. Since Amazon loads the page in an iframe, this can happen if the user has third-party
            # cookies disabled in their browser. By default Safari does this, but Chrome and Firefox do not.
            logger.error('session[\'participant_id\']=None is invalid. This could mean that third party cookies are '
                         'not enabled.')
            return render_template('sorry.html', message='An error has occurred. Please make sure that third-party '
                                                         'cookies are enabled in your browser and then reload this '
                                                         'page. (Note that by default these are disabled in Safari, but'
                                                         ' are enabled in Chrome and Firefox')
        else:
            return None

    participant = Participant.query.filter_by(id=participant_id).first()
    if participant is None and not allow_none:
        raise Exception('session[\'participant_id\']=%d is invalid.' % participant_id)

    return participant


@app.errorhandler(500)
def internal_server_error(e):
    """
    500 Internal Server Error page

    Returns
    -------
    flask.Response
    """
    return render_template('sorry.html', message='500 Internal Server Error -- Whoops... an error occurred. Sorry '
                                                 'about that. Contact us if this keeps happening. Thanks!'), 500


@app.errorhandler(404)
def page_not_found(e):
    """
    404 Page Not Found page

    Returns
    -------
    flask.Response
    """
    return render_template('sorry.html', message='404 Page Not Found -- Sorry, that page doesn\'t exist.'), 404


@app.route('/audio/<audio_file_key>.wav')
@nocache
def audio(audio_file_key):
    """
    Return audio from audio file URL in `audio_file_key`

    Parameters
    ----------
    audio_file_key: str
        The encrypted key that contains a dictionary that included an item keyed by 'path' which is the location of the
        audio file

    Returns
    -------
    flask.Response
    """
    audio_file_dict = utilities.decrypt_data(str(audio_file_key))

    # can also assert that this file is for this specific participant and condition
    assert (audio_file_dict['p_id'] == session['participant_id'])
    assert (audio_file_dict['c_id'] in session['condition_ids'])

    return send_from_directory(safe_join(app.root_path, AUDIO_FILE_DIRECTORY),
                               audio_file_dict['URL'])


@app.route('/anonymous')
@nocache
def anonymous():
    """
    This is the entry point for an anonymous participant (i.e. no external id is available)

    Returns
    -------
    flask.Response
    """
    preview = int(request.args.get('preview', 0))

    if not CONFIGURATION['anonymous_participants_enabled']:
        logger.info('Anonymous participant attempted access, but anonymous participants is disabled.')
        return render_template('sorry.html', message='This experiment is currently closed to anonymous participants.')

    return redirect(url_for('begin',
                            platform='anonymous',
                            preview=preview,
                            submission_url="",
                            crowd_worker_id="ANONYMOUS",
                            crowd_assignment_id=None,
                            crowd_assignment_type=None,
                            _external=True,
                            _scheme=URL_SCHEME))


@app.route('/mturk', methods=['GET'])
@nocache
def mturk():
    """
    This is the entry point for an Amazon Turker.

    Returns
    -------
    flask.Response
    """
    if request.args['assignmentId'] == 'ASSIGNMENT_ID_NOT_AVAILABLE' or \
                    request.args.get('workerId', 'WORKER_ID_NOT_AVAILABLE') == 'WORKER_ID_NOT_AVAILABLE':
        preview = 1
        submission_url = None
        crowd_worker_id = 'WORKER_ID_NOT_AVAILABLE'
        crowd_assignment_id = None
        crowd_assignment_type = None
    else:
        preview = 0
        submission_url = urlparse.urljoin(request.args.get('turkSubmitTo', 'TURK_SUBMIT_TO_NOT_AVAILABLE'),
                                          'mturk/externalSubmit')
        crowd_worker_id = request.args.get('workerId', 'WORKER_ID_NOT_AVAILABLE')
        crowd_assignment_id = request.args.get('assignmentId', 'ASSIGNMENT_ID_NOT_AVAILABLE')
        crowd_assignment_type = request.args.get('hitId', None)

    return redirect(url_for('begin',
                            platform='mturk',
                            crowd_worker_id=crowd_worker_id,
                            submission_url=submission_url,
                            crowd_assignment_id=crowd_assignment_id,
                            crowd_assignment_type=crowd_assignment_type,
                            preview=preview,
                            _external=True,
                            _scheme=URL_SCHEME))


@app.route('/begin/<platform>/<crowd_worker_id>', methods=['GET'])
@nocache
def begin(platform, crowd_worker_id):
    """
    Render a page with a button on it that directs them to the assign conditions. We don't direct them initially to the
    evaluation pag since some workers accept many HITs at a time. We need to make sure that they don't get assigned
    the same conditions and that their session data is valid.

    Parameters
    ----------
    platform : str
    crowd_worker_id : str

    Returns
    -------
    flask.Response
    """
    # TODO: Implement platform-specific rendering support

    # check browser
    browser = request.user_agent.browser
    if CONFIGURATION['acceptable_browsers'] is not None and browser not in CONFIGURATION['acceptable_browsers']:
        return render_template('sorry.html', message='We\'re sorry, but your web browser is not supported. Please try '
                                                     'again using <a href="http://www.google.com/chrome" '
                                                     'target="_blank">Chrome</a>.')

    # check conditions if conditions available for anyone
    conditions = experiment.get_available_conditions()
    if conditions.count() == 0:
        return render_template('sorry.html', message='We\'re sorry, but there are no more tasks available.')

    # render preview if True
    preview = int(request.args.get('preview', 0))
    if preview:
        return render_template('preview.html',
                               link="",
                               preview_html=CONFIGURATION['preview_html'],
                               **request.args)

    if CONFIGURATION['begin_button_enabled']:
        if platform == 'mturk':
            print 'mturk'
            return render_template('mturk/begin.html',
                                   link=url_for('create_participant',
                                                participant_type=platform,
                                                crowd_worker_id=crowd_worker_id,
                                                _external=True,
                                                _scheme=URL_SCHEME,
                                                **request.args),
                                   width=CONFIGURATION['popup_width'],
                                   height=CONFIGURATION['popup_height'],
                                   worker_id=crowd_worker_id,
                                   assignment_id=request.args.get('assignment_id'),
                                   hit_id=request.args.get('assignment_type'),
                                   **request.args)
        else:
            'else'
            return render_template('begin.html',
                                   link=url_for('create_participant',
                                                participant_type=platform,
                                                crowd_worker_id=crowd_worker_id,
                                                _external=True,
                                                _scheme=URL_SCHEME,
                                                **request.args),
                                   width=CONFIGURATION['popup_width'],
                                   height=CONFIGURATION['popup_height'],
                                   **request.args)
    else:
        return redirect(url_for('create_participant',
                                participant_type=platform,
                                crowd_worker_id=crowd_worker_id,
                                _external=True,
                                _scheme=URL_SCHEME,
                                **request.args))


@app.route('/participant/<participant_type>/<crowd_worker_id>')
@nocache
def create_participant(participant_type, crowd_worker_id):
    """
    Get or create participant from crowd_worker_id. Save variables to session.

    Parameters
    ----------
    participant_type : str
        The type of participant, e.g. ANONYMOUS, M_TURK, LAB, etc.
    crowd_worker_id : str
        An external identifier

    Returns
    -------
    flask.Response
    """
    session.clear()

    # Check to see if this participant has accessed CAQE before and already exists in the database
    participant = Participant.query.filter_by(crowd_worker_id=crowd_worker_id).first()

    # participant not found create new participant
    if participant is None:
        participant = Participant(participant_type, crowd_worker_id=crowd_worker_id, ip_address=request.remote_addr)
        db.session.add(participant)
        db.session.commit()
        logger.info('New Participant - %r.' % participant)
    else:
        logger.info('Participant has returned - %r' % participant)

    session['participant_id'] = participant.id
    session['crowd_data'] = {}
    # TODO: NOTE that these are platform specific.... this needs to change.
    session['crowd_data']['hit_id'] = request.args.get('hitId', None)
    session['crowd_data']['assignment_id'] = request.args.get('assignmentId', 'ASSIGNMENT_ID_NOT_AVAILABLE')
    session['crowd_data']['turk_submit_to'] = request.args.get('turkSubmitTo', 'TURK_SUBMIT_TO_NOT_AVAILABLE')

    return pre_evaluation_tasks()


def pre_evaluation_tasks():
    """
    Control overall flow of pre-evaluation tasks.
    * Assign conditions
    * Obtain consent if required
    * Present hearing screening if required
    * Present pre-test survey if required

    Returns
    -------
    flask.Response
    """
    participant = get_current_participant(session)

    # assign conditions
    session['condition_ids'] = experiment.assign_conditions(participant)

    # Are there any conditions left for the participant to do?
    if session['condition_ids'] is None or len(session['condition_ids']) == 0:
        return render_template('sorry.html', message='We\'re sorry, but there are no more tasks available for you.')

    if CONFIGURATION['obtain_consent'] and not participant.gave_consent:
        return redirect(url_for('consent', _external=True, scheme=URL_SCHEME))

    if CONFIGURATION['hearing_screening_test_enabled'] and (not participant.has_passed_hearing_test_recently()):
        return redirect(url_for('hearing_test', _external=True, scheme=URL_SCHEME))

    if CONFIGURATION['pre_test_survey_enabled']:
        if participant.pre_test_survey is None:
            return redirect(url_for('pre_test_survey', _external=True, scheme=URL_SCHEME))
        if not experiment.is_pre_test_survey_valid(json.loads(participant.pre_test_survey),
                                                   CONFIGURATION['pre_test_survey_inclusion_criteria']):
            return render_template('sorry.html',
                                   message='Unfortunately, you do not meet the inclusion criteria for this study. '
                                           'Sorry.')

    return redirect(url_for('evaluation', _external=True, _scheme=URL_SCHEME))


@app.route('/consent', methods=['GET', 'POST'])
@nocache
def consent():
    """
    Display consent page (if GET) and store results (if POST)

    Returns
    -------
    flask.Response
    """
    if request.method == 'POST':
        if request.form['consent'] == 'agree':
            participant = get_current_participant(session)
            participant.gave_consent = True
            db.session.commit()
            return pre_evaluation_tasks()
        elif request.form['consent'] == 'disagree':
            return render_template('sorry.html', message='Thank you for your interest in the study.')
        else:
            return render_template('consent.html')
    else:
        return render_template('consent.html')


@app.route('/pre_test_survey', methods=['GET', 'POST'])
@nocache
def pre_test_survey():
    """
    Display pre-test survey (if GET) and store results (if POST)

    Returns
    -------
    flask.Response
    """
    if request.method == 'POST':
        participant = get_current_participant(session)
        participant.pre_test_survey = json.dumps(request.form)
        db.session.commit()
        if experiment.is_pre_test_survey_valid(request.form, CONFIGURATION['pre_test_survey_inclusion_criteria']):
            return pre_evaluation_tasks()
        else:
            return render_template('sorry.html',
                                   message='Unfortunately, you do not meet the inclusion criteria for this study. '
                                           'Sorry.')
    else:
        return render_template('pre_test_survey.html')


@app.route('/hearing_test', methods=['GET', 'POST'])
@nocache
def hearing_test():
    """
    Determines if the user is eligible to take the hearing test (i.e. has not exceeded `MAX_HEARING_TEST_ATTEMPTS`, and
    then renders the hearing test, which consists of the assessor counting the tones in two audio files.

    If caqe.settings.HEARING_TEST_REJECTION_ENABLED is set to False, then pass them through after they had their 2
    attempts.

    Returns
    -------
    flask.Response
    """
    participant = get_current_participant(session)

    if request.method == 'GET':
        if participant.hearing_test_attempts >= CONFIGURATION['max_hearing_test_attempts']:
            logger.info('Max hearing test attempts reached - %r' % participant)
            return render_template('sorry.html', message='Sorry. You have exceed the number of allowed attempts. '
                                                         'Please try again tomorrow.')

        while True:
            hearing_test_audio_index1 = random.randint(MIN_HEARING_TEST_AUDIO_INDEX,
                                                       MAX_HEARING_TEST_AUDIO_INDEX)
            hearing_test_audio_index2 = random.randint(MIN_HEARING_TEST_AUDIO_INDEX,
                                                       MAX_HEARING_TEST_AUDIO_INDEX)
            if hearing_test_audio_index1 != hearing_test_audio_index2:
                # encrypt the data so that someone can't figure out the pattern on the client side
                logger.info('Hearing test indices %d and %d assigned to %r' %
                            (hearing_test_audio_index1, hearing_test_audio_index2, participant))
                session['hearing_test_audio_index1'] = utilities.encrypt_data(hearing_test_audio_index1)
                session['hearing_test_audio_index2'] = utilities.encrypt_data(hearing_test_audio_index2)
                break

        return render_template('hearing_screening.html')
    elif request.method == 'POST':
        try:
            hearing_test_audio_index1 = session['hearing_test_audio_index1']
            hearing_test_audio_index2 = session['hearing_test_audio_index2']
        except KeyError as e:
            hearing_test_audio_index1 = None
            hearing_test_audio_index2 = None
            logger.error("Invalid state - %r" % e)

        if (int(request.form['audiofile1_tones']) ==
                (int(utilities.decrypt_data(hearing_test_audio_index1)) / HEARING_TEST_AUDIO_FILES_PER_TONES)) \
                and (int(request.form['audiofile2_tones']) ==
                         (int(utilities.decrypt_data(hearing_test_audio_index2)) / HEARING_TEST_AUDIO_FILES_PER_TONES)):
            logger.info('Hearing test passed - %r' % participant)
            participant.set_passed_hearing_test(True)
            db.session.commit()
            return pre_evaluation_tasks()
        else:
            logger.info('Hearing test failed - %r' % participant)
            participant.set_passed_hearing_test(False)
            db.session.commit()

            if participant.hearing_test_attempts < CONFIGURATION['max_hearing_test_attempts']:
                flash('You answered incorrectly. If you are unable to pass this test, it is likely that your output '
                      'device (e.g. your headphones) is not producing the full range of frequencies required for this '
                      'task. Try using better headphones.', 'danger')
            else:
                if not CONFIGURATION['hearing_test_rejection_enabled']:
                    # They attempted, but they failed, but pass them through since rejection is not enabled
                    logger.info('Hearing test rejection enabled. Passing failed participant to evaluation.')
                    return pre_evaluation_tasks()
            return redirect(url_for('hearing_test', _method='GET', _external=True, _scheme=URL_SCHEME))


@app.route('/hearing_test/audio/<example_num>.wav')
@nocache
def hearing_test_audio(example_num):
    """
    Retrieve audio for hearing test

    Parameters
    ----------
    example_num : str
        The index of the example audio (1 or 2)

    Return
    ------
    flask.Response
    """
    if example_num == '0':
        # calibration file
        file_path = 'hearing_test_audio/1000Hz.wav'
    else:
        hearing_test_audio_index = int(utilities.decrypt_data(session['hearing_test_audio_index%s' % example_num]))
        num_tones = hearing_test_audio_index / HEARING_TEST_AUDIO_FILES_PER_TONES
        file_num = hearing_test_audio_index % HEARING_TEST_AUDIO_FILES_PER_TONES
        logger.info('hearing_test %s - %d %d' % (example_num, num_tones, file_num))
        file_path = 'hearing_test_audio/tones%d_%d.wav' % (num_tones, file_num)
    with open(file_path, 'rb') as f:
        response = make_response(f.read())
        response.headers['Content-Type'] = 'audio/wav'
        response.headers['Accept-Ranges'] = 'bytes'
    return response


@app.route('/evaluation', methods=['GET', 'POST'])
@nocache
def evaluation():
    """
    Renders the listening test (if GET) and saves the results (if POST)

    Returns
    -------
    flask.Response
    """
    participant = get_current_participant(session)

    if request.method == 'POST':
        # SAVE DATA
        try:
            # get relevant data
            participant = get_current_participant(session)
            crowd_data = session.get('crowd_data', None)
            participant_id = int(request.values['participant_id'])
            # ensure that the participant_id is correct
            assert (participant.id == participant_id)

            condition_data = json.loads(request.values['completedConditionData'])
            for cd in condition_data:
                # get data
                condition_id = int(cd['conditionID'])

                # decrypt audio stimuli
                cd = experiment.decrypt_audio_stimuli(cd)

                # create database object
                trial = Trial(participant_id, condition_id, json.dumps(cd), json.dumps(crowd_data),
                              participant.passed_hearing_test)
                db.session.add(trial)
                logger.info('Results saved for %r' % trial)

            db.session.commit()
            return json.dumps({'error': False, 'message': 'Data is saved!', 'trial_id': utilities.sign_data(trial.id)})
        except Exception as e:
            logger.warning('Error saving results. - %r' % e)
            return json.dumps({'error': True, 'message': 'Error saving data. Error %r' % utilities.sign_data(str(e))})
    else:
        test_configurations = experiment.get_test_configurations(session['condition_ids'], participant.id)

        # for now don't consider the case that there could be more than one test per participant
        assert len(
            test_configurations) == 1, "`test_configuration` has length greater than 1. This is not supported for now."
        test_config = test_configurations[0]

        if test_config['test']['test_type'] == 'mushra':
            return render_template('mushra.html',
                                   test=test_config['test'],
                                   conditions=test_config['conditions'],
                                   participant_id=participant.id,
                                   first_evaluation=participant.trials.count() == 0,
                                   test_complete_redirect_url=url_for('post_evaluation_tasks', _external=True,
                                                                      _scheme=URL_SCHEME),
                                   submission_url=url_for('evaluation', _external=True, _scheme=URL_SCHEME))
        elif test_config['test']['test_type'] == 'pairwise':
            test_config['conditions'] = experiment.generate_comparison_pairs(test_config['conditions'])

            return render_template('pairwise.html',
                                   test=test_config['test'],
                                   conditions=test_config['conditions'],
                                   participant_id=participant.id,
                                   first_evaluation=participant.trials.count() == 0,
                                   test_complete_redirect_url=url_for('post_evaluation_tasks', _external=True,
                                                                      _scheme=URL_SCHEME),
                                   submission_url=url_for('evaluation', _external=True, _scheme=URL_SCHEME))
        else:
            return render_template('%s.html' % test_config['test']['test_type'],
                                   test=test_config['test'],
                                   conditions=test_config['conditions'],
                                   participant_id=participant.id,
                                   first_evaluation=participant.trials.count() == 0,
                                   test_complete_redirect_url=url_for('post_evaluation_tasks', _external=True,
                                                                      _scheme=URL_SCHEME),
                                   submission_url=url_for('evaluation', _external=True, _scheme=URL_SCHEME))


@app.route('/post_evaluation_tasks')
@nocache
def post_evaluation_tasks():
    """
    Control overall flow of post-evaluation tasks.
    * Present hearing response estimation if required
    * Present post-test survey if required
    * Present thank you page and submit task if required

    Returns
    -------
    flask.Response
    """

    participant = get_current_participant(session)

    if CONFIGURATION['hearing_response_estimation_enabled'] and participant.hearing_response_estimation is None:
        return redirect(url_for('hearing_response_estimation', _external=True, scheme=URL_SCHEME))

    if CONFIGURATION['post_test_survey_enabled'] and participant.post_test_survey is None:
        return redirect(url_for('post_test_survey', _external=True, scheme=URL_SCHEME))

    platform = session.get('platform', None)
    return redirect(url_for('end',
                            platform=platform,
                            _external=True,
                            _scheme=URL_SCHEME))


@app.route('/hearing_response_estimation', methods=['GET', 'POST'])
@nocache
def hearing_response_estimation():
    """
    Perform in-situ hearing response estimation (if GET) and store results (if POST)

    Returns
    -------
    flask.Response
    """
    if request.method == 'POST':
        participant = get_current_participant(session)
        participant.hearing_response_estimation = json.dumps(request.form)
        db.session.commit()
        return post_evaluation_tasks()
    else:
        hearing_response_file_path = url_for('static',
                                             filename='audio/hearing_response_stimuli/',
                                             _external=True,
                                             _scheme=URL_SCHEME)

        hearing_response_file_path = strip_query_from_url(hearing_response_file_path)

        freq_seq = range(CONFIGURATION['hearing_response_nfreqs'])
        random.shuffle(freq_seq)

        hearing_response_ids = []
        hearing_response_files = []
        for f in freq_seq:
            hearing_response_id = '%d_%d' % (f, random.randint(0, CONFIGURATION['hearing_response_nadd']))
            hearing_response_file = '%s%s.wav' % (hearing_response_file_path, hearing_response_id)
            hearing_response_ids.append(hearing_response_id)
            hearing_response_files.append(hearing_response_file)

        return render_template('hearing_response_estimation.html',
                               hearing_response_ids=hearing_response_ids,
                               hearing_response_files=hearing_response_files,
                               n_options=CONFIGURATION['n_options'])


@app.route('/post_test_survey', methods=['GET', 'POST'])
@nocache
def post_test_survey():
    """
    Display post-test survey (if GET) and store results (if POST)

    Returns
    -------
    flask.Response
    """
    if request.method == 'POST':
        participant = get_current_participant(session)
        participant.post_test_survey = json.dumps(request.form)
        db.session.commit()
        return post_evaluation_tasks()
    else:
        return render_template('post_test_survey.html')


@app.route('/end/<platform>', methods=['GET'])
@nocache
def end(platform):
    """
    Render a thank you page with a button on it that directs submits their task or simply closes the window (depending
    on the platform)

    Parameters
    ----------
    platform : str

    Returns
    -------
    flask.Response
    """
    if platform == 'mturk':
        return render_template('mturk/end.html')
    else:
        return render_template('end.html')


# ADMINISTRATIVE AND TESTING VIEWS
@app.route('/mturk_debug', methods=['GET'])
@nocache
def mturk_debug():
    """
    This just a view for previewing what the page would look like on Mechanical Turk


    Returns
    -------
    flask.Response
    """
    preview = int(request.args.get('preview', 0))
    if preview:
        return render_template('mturk_debug.html',
                               url='/mturk?assignmentId=ASSIGNMENT_ID_NOT_AVAILABLE&workerId=debugNQFUCL',
                               frame_height=MTURK_DEBUG_FRAME_HEIGHT)
    else:
        return render_template('mturk_debug.html',
                               url='/mturk?assignmentId=123RVWYBAZW00EXAMPLE456RVWYBAZW00EXAMPLE&'
                                   'hitId=123RVWYBAZW00EXAMPLE&'
                                   'turkSubmitTo=https://workersandbox.mturk.com&'
                                   'workerId=debugNQFUCL',
                               frame_height=MTURK_DEBUG_FRAME_HEIGHT)


@app.route('/admin/stats')
@nocache
def admin_stats():
    trials = Trial.query.all()
    conditions = Condition.query.all()
    passed_hearing_condition_count = dict([(cond.id, 0) for cond in conditions])
    failed_hearing_condition_count = dict([(cond.id, 0) for cond in conditions])

    for trial in trials:
        if trial.participant_passed_hearing_test:
            passed_hearing_condition_count[trial.condition_id] += 1
        else:
            failed_hearing_condition_count[trial.condition_id] += 1

    fieldnames = ['Condition', 'Completed Trials (passed hearing test)', 'Completed Trials (failed hearing test)']
    ids = sorted(passed_hearing_condition_count.keys())
    rows = [{'Condition': i,
             'Completed Trials (passed hearing test)': passed_hearing_condition_count[i],
             'Completed Trials (failed hearing test)': failed_hearing_condition_count[i]} for i in ids]
    title = 'Trial Statistics'
    return render_template('table.html',
                           fieldnames=fieldnames,
                           rows=rows,
                           title=title)


@app.route('/bonus')
@nocache
def bonus():
    worker_id = request.args.get('workerId', 'WORKER_ID_NOT_AVAILABLE')
    hit_id = request.args.get('hitId', None)
    assignment_id = request.args['assignmentId']
    turk_submit_to = urlparse.urljoin(request.args.get('turkSubmitTo', 'TURK_SUBMIT_TO_NOT_AVAILABLE'),
                                      '/mturk/externalSubmit')

    return render_template('bonus.html',
                           turk_submit_to=turk_submit_to,
                           worker_id=worker_id,
                           hit_id=hit_id,
                           assignment_id=assignment_id,
                           preview=['false', 'true'][assignment_id == 'ASSIGNMENT_ID_NOT_AVAILABLE'])
