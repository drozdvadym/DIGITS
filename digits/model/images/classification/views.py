# Copyright (c) 2014-2016, NVIDIA CORPORATION.  All rights reserved.
from __future__ import absolute_import

import os
import random
import re
import tempfile

import flask
import numpy as np
import werkzeug.exceptions

from .forms import ImageClassificationModelForm
from .job import ImageClassificationModelJob
import digits
from digits import frameworks
from digits import utils
from digits.config import config_value
from digits.dataset import ImageClassificationDatasetJob
from digits.inference import ImageInferenceJob
from digits.status import Status
from digits.utils import filesystem as fs
from digits.utils.forms import fill_form_if_cloned, save_form_to_job
from digits.utils.routing import request_wants_json, job_from_request
from digits.webapp import app, scheduler

blueprint = flask.Blueprint(__name__, __name__)

"""
Read image list
"""
def read_image_list(image_list, image_folder, num_test_images):
    paths = []
    ground_truths = []

    for line in image_list.readlines():
        line = line.strip()
        if not line:
            continue

        # might contain a numerical label at the end
        match = re.match(r'(.*\S)\s+(\d+)$', line)
        if match:
            path = match.group(1)
            ground_truth = int(match.group(2))
        else:
            path = line
            ground_truth = None

        if not utils.is_url(path) and image_folder and not os.path.isabs(path):
            path = os.path.join(image_folder, path)
        paths.append(path)
        ground_truths.append(ground_truth)

        if num_test_images is not None and len(paths) >= num_test_images:
            break
    return paths, ground_truths

@blueprint.route('/new', methods=['GET'])
@utils.auth.requires_login
def new():
    """
    Return a form for a new ImageClassificationModelJob
    """
    form = ImageClassificationModelForm()
    form.dataset.choices = get_datasets()
    form.standard_networks.choices = get_standard_networks()
    form.standard_networks.default = get_default_standard_network()
    form.previous_networks.choices = get_previous_networks()

    prev_network_snapshots = get_previous_network_snapshots()

    ## Is there a request to clone a job with ?clone=<job_id>
    fill_form_if_cloned(form)

    return flask.render_template('models/images/classification/new.html',
            form = form,
            frameworks = frameworks.get_frameworks(),
            previous_network_snapshots = prev_network_snapshots,
            previous_networks_fullinfo = get_previous_networks_fulldetails(),
            multi_gpu = config_value('caffe_root')['multi_gpu'],
            )

@blueprint.route('.json', methods=['POST'])
@blueprint.route('', methods=['POST'], strict_slashes=False)
@utils.auth.requires_login(redirect=False)
def create():
    """
    Create a new ImageClassificationModelJob

    Returns JSON when requested: {job_id,name,status} or {errors:[]}
    """
    form = ImageClassificationModelForm()
    form.dataset.choices = get_datasets()
    form.standard_networks.choices = get_standard_networks()
    form.standard_networks.default = get_default_standard_network()
    form.previous_networks.choices = get_previous_networks()

    prev_network_snapshots = get_previous_network_snapshots()

    ## Is there a request to clone a job with ?clone=<job_id>
    fill_form_if_cloned(form)

    if not form.validate_on_submit():
        if request_wants_json():
            return flask.jsonify({'errors': form.errors}), 400
        else:
            return flask.render_template('models/images/classification/new.html',
                    form = form,
                    frameworks = frameworks.get_frameworks(),
                    previous_network_snapshots = prev_network_snapshots,
                    previous_networks_fullinfo = get_previous_networks_fulldetails(),
                    multi_gpu = config_value('caffe_root')['multi_gpu'],
                    ), 400

    datasetJob = scheduler.get_job(form.dataset.data)
    if not datasetJob:
        raise werkzeug.exceptions.BadRequest(
                'Unknown dataset job_id "%s"' % form.dataset.data)

    job = None
    try:
        job = ImageClassificationModelJob(
                username    = utils.auth.get_username(),
                name        = form.model_name.data,
                dataset_id  = datasetJob.id(),
                )
        # get handle to framework object
        fw = frameworks.get_framework_by_id(form.framework.data)

        pretrained_model = None
        if form.method.data == 'standard':
            found = False

            # can we find it in standard networks?
            network_desc = fw.get_standard_network_desc(form.standard_networks.data)
            if network_desc:
                found = True
                network = fw.get_network_from_desc(network_desc)

            if not found:
                raise werkzeug.exceptions.BadRequest(
                        'Unknown standard model "%s"' % form.standard_networks.data)
        elif form.method.data == 'previous':
            old_job = scheduler.get_job(form.previous_networks.data)
            if not old_job:
                raise werkzeug.exceptions.BadRequest(
                        'Job not found: %s' % form.previous_networks.data)

            use_same_dataset = (old_job.dataset_id == job.dataset_id)
            network = fw.get_network_from_previous(old_job.train_task().network, use_same_dataset)

            for choice in form.previous_networks.choices:
                if choice[0] == form.previous_networks.data:
                    epoch = float(flask.request.form['%s-snapshot' % form.previous_networks.data])
                    if epoch == 0:
                        pass
                    elif epoch == -1:
                        pretrained_model = old_job.train_task().pretrained_model
                    else:
                        for filename, e in old_job.train_task().snapshots:
                            if e == epoch:
                                pretrained_model = filename
                                break

                        if pretrained_model is None:
                            raise werkzeug.exceptions.BadRequest(
                                    "For the job %s, selected pretrained_model for epoch %d is invalid!"
                                    % (form.previous_networks.data, epoch))
                        if not (os.path.exists(pretrained_model)):
                            raise werkzeug.exceptions.BadRequest(
                                    "Pretrained_model for the selected epoch doesn't exists. May be deleted by another user/process. Please restart the server to load the correct pretrained_model details")
                    break

        elif form.method.data == 'custom':
            network = fw.get_network_from_desc(form.custom_network.data)
            pretrained_model = form.custom_network_snapshot.data.strip()
        else:
            raise werkzeug.exceptions.BadRequest(
                    'Unrecognized method: "%s"' % form.method.data)

        policy = {'policy': form.lr_policy.data}
        if form.lr_policy.data == 'fixed':
            pass
        elif form.lr_policy.data == 'step':
            policy['stepsize'] = form.lr_step_size.data
            policy['gamma'] = form.lr_step_gamma.data
        elif form.lr_policy.data == 'multistep':
            policy['stepvalue'] = form.lr_multistep_values.data
            policy['gamma'] = form.lr_multistep_gamma.data
        elif form.lr_policy.data == 'exp':
            policy['gamma'] = form.lr_exp_gamma.data
        elif form.lr_policy.data == 'inv':
            policy['gamma'] = form.lr_inv_gamma.data
            policy['power'] = form.lr_inv_power.data
        elif form.lr_policy.data == 'poly':
            policy['power'] = form.lr_poly_power.data
        elif form.lr_policy.data == 'sigmoid':
            policy['stepsize'] = form.lr_sigmoid_step.data
            policy['gamma'] = form.lr_sigmoid_gamma.data
        else:
            raise werkzeug.exceptions.BadRequest(
                    'Invalid learning rate policy')

        if config_value('caffe_root')['multi_gpu']:
            if form.select_gpus.data:
                selected_gpus = [str(gpu) for gpu in form.select_gpus.data]
                gpu_count = None
            elif form.select_gpu_count.data:
                gpu_count = form.select_gpu_count.data
                selected_gpus = None
            else:
                gpu_count = 1
                selected_gpus = None
        else:
            if form.select_gpu.data == 'next':
                gpu_count = 1
                selected_gpus = None
            else:
                selected_gpus = [str(form.select_gpu.data)]
                gpu_count = None

        # Python Layer File may be on the server or copied from the client.
        fs.copy_python_layer_file(
            bool(form.python_layer_from_client.data),
            job.dir(),
            (flask.request.files[form.python_layer_client_file.name]
             if form.python_layer_client_file.name in flask.request.files
             else ''), form.python_layer_server_file.data)

        job.tasks.append(fw.create_train_task(
                    job_dir         = job.dir(),
                    dataset         = datasetJob,
                    train_epochs    = form.train_epochs.data,
                    snapshot_interval   = form.snapshot_interval.data,
                    learning_rate   = form.learning_rate.data,
                    lr_policy       = policy,
                    gpu_count       = gpu_count,
                    selected_gpus   = selected_gpus,
                    batch_size      = form.batch_size.data,
                    iter_size       = form.iter_size.data,
                    val_interval    = form.val_interval.data,
                    pretrained_model= pretrained_model,
                    crop_size       = form.crop_size.data,
                    use_mean        = form.use_mean.data,
                    network         = network,
                    random_seed     = form.random_seed.data,
                    solver_type     = form.solver_type.data,
                    shuffle         = form.shuffle.data,
                    )
                )

        ## Save form data with the job so we can easily clone it later.
        save_form_to_job(job, form)

        scheduler.add_job(job)
        if request_wants_json():
            return flask.jsonify(job.json_dict())
        else:
            return flask.redirect(flask.url_for('digits.model.views.show', job_id=job.id()))

    except:
        if job:
            scheduler.delete_job(job)
        raise

def show(job):
    """
    Called from digits.model.views.models_show()
    """
    return flask.render_template('models/images/classification/show.html', job=job, framework_ids = [fw.get_id() for fw in frameworks.get_frameworks()])

@blueprint.route('/large_graph', methods=['GET'])
def large_graph():
    """
    Show the loss/accuracy graph, but bigger
    """
    job = job_from_request()

    return flask.render_template('models/images/classification/large_graph.html', job=job)

@blueprint.route('/classify_one.json', methods=['POST'])
@blueprint.route('/classify_one', methods=['POST', 'GET'])
def classify_one():
    """
    Classify one image and return the top 5 classifications

    Returns JSON when requested: {predictions: {category: confidence,...}}
    """
    model_job = job_from_request()

    remove_image_path = False
    if 'image_path' in flask.request.form and flask.request.form['image_path']:
        image_path = flask.request.form['image_path']
    elif 'image_file' in flask.request.files and flask.request.files['image_file']:
        outfile = tempfile.mkstemp(suffix='.png')
        flask.request.files['image_file'].save(outfile[1])
        image_path = outfile[1]
        os.close(outfile[0])
        remove_image_path = True
    else:
        raise werkzeug.exceptions.BadRequest('must provide image_path or image_file')

    epoch = None
    if 'snapshot_epoch' in flask.request.form:
        epoch = float(flask.request.form['snapshot_epoch'])

    layers = 'none'
    if 'show_visualizations' in flask.request.form and flask.request.form['show_visualizations']:
        layers = 'all'

    # create inference job
    inference_job = ImageInferenceJob(
                username    = utils.auth.get_username(),
                name        = "Classify One Image",
                model       = model_job,
                images      = [image_path],
                epoch       = epoch,
                layers      = layers
                )

    # schedule tasks
    scheduler.add_job(inference_job)

    # wait for job to complete
    inference_job.wait_completion()

    # retrieve inference data
    inputs, outputs, visualizations = inference_job.get_data()

    # delete job
    scheduler.delete_job(inference_job)

    if remove_image_path:
        os.remove(image_path)

    image = None
    predictions = []
    if inputs is not None and len(inputs['data']) == 1:
        image = utils.image.embed_image_html(inputs['data'][0])
        # convert to class probabilities for viewing
        last_output_name, last_output_data = outputs.items()[-1]

        if len(last_output_data) == 1:
            scores = last_output_data[0].flatten()
            indices = (-scores).argsort()
            labels = model_job.train_task().get_labels()
            predictions = []
            for i in indices:
                predictions.append( (labels[i], scores[i]) )
            predictions = [(p[0], round(100.0*p[1],2)) for p in predictions[:5]]

    if request_wants_json():
        return flask.jsonify({'predictions': predictions})
    else:
        return flask.render_template('models/images/classification/classify_one.html',
                model_job       = model_job,
                job             = inference_job,
                image_src       = image,
                predictions     = predictions,
                visualizations  = visualizations,
                total_parameters= sum(v['param_count'] for v in visualizations if v['vis_type'] == 'Weights'),
                )

@blueprint.route('/classify_many.json', methods=['POST'])
@blueprint.route('/classify_many', methods=['POST', 'GET'])
def classify_many():
    """
    Classify many images and return the top 5 classifications for each

    Returns JSON when requested: {classifications: {filename: [[category,confidence],...],...}}
    """
    model_job = job_from_request()

    image_list = flask.request.files.get('image_list')
    if not image_list:
        raise werkzeug.exceptions.BadRequest('image_list is a required field')

    if 'image_folder' in flask.request.form and flask.request.form['image_folder'].strip():
        image_folder = flask.request.form['image_folder']
        if not os.path.exists(image_folder):
            raise werkzeug.exceptions.BadRequest('image_folder "%s" does not exit' % image_folder)
    else:
        image_folder = None

    if 'num_test_images' in flask.request.form and flask.request.form['num_test_images'].strip():
        num_test_images = int(flask.request.form['num_test_images'])
    else:
        num_test_images = None

    epoch = None
    if 'snapshot_epoch' in flask.request.form:
        epoch = float(flask.request.form['snapshot_epoch'])

    paths, ground_truths = read_image_list(image_list, image_folder, num_test_images)

    # create inference job
    inference_job = ImageInferenceJob(
                username    = utils.auth.get_username(),
                name        = "Classify Many Images",
                model       = model_job,
                images      = paths,
                epoch       = epoch,
                layers      = 'none'
                )

    # schedule tasks
    scheduler.add_job(inference_job)

    # wait for job to complete
    inference_job.wait_completion()

    # retrieve inference data
    inputs, outputs, _ = inference_job.get_data()

    # delete job
    scheduler.delete_job(inference_job)

    if outputs is not None and len(outputs) < 1:
        # an error occurred
        outputs = None

    if inputs is not None:
        # retrieve path and ground truth of images that were successfully processed
        paths = [paths[idx] for idx in inputs['ids']]
        ground_truths = [ground_truths[idx] for idx in inputs['ids']]

    # defaults
    classifications = None
    show_ground_truth = None
    top1_accuracy = None
    top5_accuracy = None
    confusion_matrix = None
    per_class_accuracy = None
    labels = None

    if outputs is not None:
        # convert to class probabilities for viewing
        last_output_name, last_output_data = outputs.items()[-1]
        if len(last_output_data) < 1:
            raise werkzeug.exceptions.BadRequest(
                    'Unable to classify any image from the file')

        scores = last_output_data
        # take top 5
        indices = (-scores).argsort()[:, :5]

        labels = model_job.train_task().get_labels()
        n_labels = len(labels)

        # remove invalid ground truth
        ground_truths = [x if x is not None and (0 <= x < n_labels) else None for x in ground_truths]

        # how many pieces of ground truth to we have?
        n_ground_truth = len([1 for x in ground_truths if x is not None])
        show_ground_truth = n_ground_truth > 0

        # compute classifications and statistics
        classifications = []
        n_top1_accurate = 0
        n_top5_accurate = 0
        confusion_matrix = np.zeros((n_labels,n_labels), dtype=np.dtype(int))
        for image_index, index_list in enumerate(indices):
            result = []
            if ground_truths[image_index] is not None:
                if ground_truths[image_index] == index_list[0]:
                    n_top1_accurate += 1
                if ground_truths[image_index] in index_list:
                    n_top5_accurate += 1
                if (0 <= ground_truths[image_index] < n_labels) and (0 <= index_list[0] < n_labels):
                   confusion_matrix[ground_truths[image_index], index_list[0]] += 1
            for i in index_list:
                # `i` is a category in labels and also an index into scores
                result.append((labels[i], round(100.0*scores[image_index, i],2)))
            classifications.append(result)

        # accuracy
        if show_ground_truth:
            top1_accuracy = round(100.0 * n_top1_accurate / n_ground_truth, 2)
            top5_accuracy = round(100.0 * n_top5_accurate / n_ground_truth, 2)
            per_class_accuracy = []
            for x in xrange(n_labels):
                n_examples = sum(confusion_matrix[x])
                per_class_accuracy.append(round(100.0 * confusion_matrix[x,x] / n_examples, 2) if n_examples > 0 else None)
        else:
            top1_accuracy = None
            top5_accuracy = None
            per_class_accuracy = None

        # replace ground truth indices with labels
        ground_truths = [labels[x] if x is not None and (0 <= x < n_labels ) else None for x in ground_truths]

    if request_wants_json():
        joined = dict(zip(paths, classifications))
        return flask.jsonify({'classifications': joined})
    else:
        return flask.render_template('models/images/classification/classify_many.html',
                model_job          = model_job,
                job                = inference_job,
                paths              = paths,
                classifications    = classifications,
                show_ground_truth  = show_ground_truth,
                ground_truths      = ground_truths,
                top1_accuracy      = top1_accuracy,
                top5_accuracy      = top5_accuracy,
                confusion_matrix   = confusion_matrix,
                per_class_accuracy = per_class_accuracy,
                labels             = labels,
                )

@blueprint.route('/top_n', methods=['POST'])
def top_n():
    """
    Classify many images and show the top N images per category by confidence
    """
    model_job = job_from_request()

    image_list = flask.request.files['image_list']
    if not image_list:
        raise werkzeug.exceptions.BadRequest('File upload not found')

    epoch = None
    if 'snapshot_epoch' in flask.request.form:
        epoch = float(flask.request.form['snapshot_epoch'])
    if 'top_n' in flask.request.form and flask.request.form['top_n'].strip():
        top_n = int(flask.request.form['top_n'])
    else:
        top_n = 9

    if 'image_folder' in flask.request.form and flask.request.form['image_folder'].strip():
        image_folder = flask.request.form['image_folder']
        if not os.path.exists(image_folder):
            raise werkzeug.exceptions.BadRequest('image_folder "%s" does not exit' % image_folder)
    else:
        image_folder = None

    if 'num_test_images' in flask.request.form and flask.request.form['num_test_images'].strip():
        num_test_images = int(flask.request.form['num_test_images'])
    else:
        num_test_images = None

    paths, _ = read_image_list(image_list, image_folder, num_test_images)

    # create inference job
    inference_job = ImageInferenceJob(
                username    = utils.auth.get_username(),
                name        = "TopN Image Classification",
                model       = model_job,
                images      = paths,
                epoch       = epoch,
                layers      = 'none'
                )

    # schedule tasks
    scheduler.add_job(inference_job)

    # wait for job to complete
    inference_job.wait_completion()

    # retrieve inference data
    inputs, outputs, _ = inference_job.get_data()

    # delete job
    scheduler.delete_job(inference_job)

    results = None
    if outputs is not None and len(outputs) > 0:
        # convert to class probabilities for viewing
        last_output_name, last_output_data = outputs.items()[-1]
        scores = last_output_data

        if scores is None:
            raise RuntimeError('An error occured while processing the images')

        labels = model_job.train_task().get_labels()
        images = inputs['data']
        indices = (-scores).argsort(axis=0)[:top_n]
        results = []
        # Can't have more images per category than the number of images
        images_per_category = min(top_n, len(images))
        for i in xrange(indices.shape[1]):
            result_images = []
            for j in xrange(images_per_category):
                result_images.append(images[indices[j][i]])
            results.append((
                    labels[i],
                    utils.image.embed_image_html(
                        utils.image.vis_square(np.array(result_images),
                            colormap='white')
                        )
                    ))

    return flask.render_template('models/images/classification/top_n.html',
            model_job       = model_job,
            job             = inference_job,
            results         = results,
            )

def get_datasets():
    return [(j.id(), j.name()) for j in sorted(
        [j for j in scheduler.jobs.values() if isinstance(j, ImageClassificationDatasetJob) and (j.status.is_running() or j.status == Status.DONE)],
        cmp=lambda x,y: cmp(y.id(), x.id())
        )
        ]

def get_standard_networks():
    return [
            ('lenet', 'LeNet'),
            ('alexnet', 'AlexNet'),
            #('vgg-16', 'VGG (16-layer)'), #XXX model won't learn
            ('googlenet', 'GoogLeNet'),
            ]

def get_default_standard_network():
    return 'alexnet'

def get_previous_networks():
    return [(j.id(), j.name()) for j in sorted(
        [j for j in scheduler.jobs.values() if isinstance(j, ImageClassificationModelJob)],
        cmp=lambda x,y: cmp(y.id(), x.id())
        )
        ]

def get_previous_networks_fulldetails():
    return [(j) for j in sorted(
        [j for j in scheduler.jobs.values() if isinstance(j, ImageClassificationModelJob)],
        cmp=lambda x,y: cmp(y.id(), x.id())
        )
        ]

def get_previous_network_snapshots():
    prev_network_snapshots = []
    for job_id, _ in get_previous_networks():
        job = scheduler.get_job(job_id)
        e = [(0, 'None')] + [(epoch, 'Epoch #%s' % epoch)
                for _, epoch in reversed(job.train_task().snapshots)]
        if job.train_task().pretrained_model:
            e.insert(0, (-1, 'Previous pretrained model'))
        prev_network_snapshots.append(e)
    return prev_network_snapshots

