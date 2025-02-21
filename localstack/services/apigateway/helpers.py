import re
import json
from requests.models import Response
from six.moves.urllib import parse as urlparse
from localstack.utils import common
from localstack.constants import TEST_AWS_ACCOUNT_ID, APPLICATION_JSON
from localstack.utils.aws import aws_stack
from localstack.utils.aws.aws_responses import requests_response

# regex path patterns
PATH_REGEX_MAIN = r'^/restapis/([A-Za-z0-9_\-]+)/[a-z]+(\?.*)?'
PATH_REGEX_SUB = r'^/restapis/([A-Za-z0-9_\-]+)/[a-z]+/([A-Za-z0-9_\-]+)/.*'

# template for SQS inbound data
APIGATEWAY_SQS_DATA_INBOUND_TEMPLATE = "Action=SendMessage&MessageBody=$util.base64Encode($input.json('$'))"

# maps API ids to authorizers
AUTHORIZERS = {}


def make_response(message):
    return requests_response(json.dumps(message), headers={'Content-Type': APPLICATION_JSON})


def make_error(message, code=400):
    response = Response()
    response.status_code = code
    response._content = json.dumps({'message': message})
    return response


def get_api_id_from_path(path):
    match = re.match(PATH_REGEX_SUB, path)
    if match:
        return match.group(1)
    return re.match(PATH_REGEX_MAIN, path).group(1)


def get_authorizers(path):
    result = {'item': []}
    api_id = get_api_id_from_path(path)
    for key, value in AUTHORIZERS.items():
        auth_api_id = get_api_id_from_path(value['_links']['self']['href'])
        if auth_api_id == api_id:
            result['item'].append(value)
    return result


def add_authorizer(path, data):
    api_id = get_api_id_from_path(path)
    result = common.clone(data)
    result['id'] = common.short_uid()
    if '_links' not in result:
        result['_links'] = {}
    result['_links']['self'] = {
        'href': '/restapis/%s/authorizers/%s' % (api_id, result['id'])
    }
    AUTHORIZERS[result['id']] = result
    return result


def handle_authorizers(method, path, data, headers):
    result = {}
    if method == 'GET':
        result = get_authorizers(path)
    elif method == 'POST':
        result = add_authorizer(path, data)
    else:
        return make_error('Not implemented for API Gateway authorizers: %s' % method, 404)
    return make_response(result)


def tokenize_path(path):
    return path.lstrip('/').split('/')


def extract_path_params(path, extracted_path):
    tokenized_extracted_path = tokenize_path(extracted_path)
    # Looks for '{' in the tokenized extracted path
    path_params_list = [(i, v) for i, v in enumerate(tokenized_extracted_path) if '{' in v]
    tokenized_path = tokenize_path(path)
    path_params = {}
    for param in path_params_list:
        path_param_name = param[1][1:-1].encode('utf-8')
        path_param_position = param[0]
        if path_param_name.endswith(b'+'):
            path_params[path_param_name] = '/'.join(tokenized_path[path_param_position:])
        else:
            path_params[path_param_name] = tokenized_path[path_param_position]
    path_params = common.json_safe(path_params)
    return path_params


def extract_query_string_params(path):
    parsed_path = urlparse.urlparse(path)
    path = parsed_path.path
    parsed_query_string_params = urlparse.parse_qs(parsed_path.query)

    query_string_params = {}
    for query_param_name, query_param_values in parsed_query_string_params.items():
        if len(query_param_values) == 1:
            query_string_params[query_param_name] = query_param_values[0]
        else:
            query_string_params[query_param_name] = query_param_values

    return [path, query_string_params]


def get_cors_response(headers):
    # TODO: for now we simply return "allow-all" CORS headers, but in the future
    # we should implement custom headers for CORS rules, as supported by API Gateway:
    # http://docs.aws.amazon.com/apigateway/latest/developerguide/how-to-cors.html
    response = Response()
    response.status_code = 200
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE'
    response.headers['Access-Control-Allow-Headers'] = '*'
    response._content = ''
    return response


def get_rest_api_paths(rest_api_id, region_name=None):
    apigateway = aws_stack.connect_to_service(service_name='apigateway', region_name=region_name)
    resources = apigateway.get_resources(restApiId=rest_api_id, limit=100)
    resource_map = {}
    for resource in resources['items']:
        path = aws_stack.get_apigateway_path_for_resource(rest_api_id, resource['id'], region_name=region_name)
        resource_map[path] = resource
    return resource_map


def get_resource_for_path(path, path_map):
    matches = []
    for api_path, details in path_map.items():
        api_path_regex = re.sub(r'\{[^\+]+\+\}', r'[^\?#]+', api_path)
        api_path_regex = re.sub(r'\{[^\}]+\}', r'[^/]+', api_path_regex)
        if re.match(r'^%s$' % api_path_regex, path):
            matches.append((api_path, details))
    if not matches:
        return None
    if len(matches) > 1:
        # check if we have an exact match
        for match in matches:
            if match[0] == path:
                return match
        raise Exception('Ambiguous API path %s - matches found: %s' % (path, matches))
    return matches[0]


def connect_api_gateway_to_sqs(gateway_name, stage_name, queue_arn, path, region_name=None):
    resources = {}
    template = APIGATEWAY_SQS_DATA_INBOUND_TEMPLATE
    resource_path = path.replace('/', '')
    region_name = region_name or aws_stack.get_region()
    queue_name = aws_stack.sqs_queue_name(queue_arn)
    sqs_region = aws_stack.extract_region_from_arn(queue_arn) or region_name
    resources[resource_path] = [{
        'httpMethod': 'POST',
        'authorizationType': 'NONE',
        'integrations': [{
            'type': 'AWS',
            'uri': 'arn:aws:apigateway:%s:sqs:path/%s/%s' % (
                sqs_region, TEST_AWS_ACCOUNT_ID, queue_name
            ),
            'requestTemplates': {
                'application/json': template
            },
        }]
    }]
    return aws_stack.create_api_gateway(
        name=gateway_name, resources=resources, stage_name=stage_name, region_name=region_name)
