import urllib2
import re
from zipfile import ZipFile
from io import BytesIO
import json

import boto3

RUNTIME = 'python2.7'

graph = ["digraph lambda {"]
graph.append("overlap=scalexy;")
graph.append("sep=0.1;")

with open('style.json') as style_file:
    style_map = json.loads(style_file.read())

def build_node(name, node_type):
    return ('"{}" [shape={} fillcolor={} style={}];'.format(
        name,
        style_map[node_type]['shape'],
        style_map[node_type]['color'],
        style_map[node_type]['style']
    ))


## API GATEWAY #################################################################

api_client = boto3.client('apigateway')
apis = {}

resp = api_client.get_rest_apis()
for api in resp['items']:
    apis[api['name']] = {'id' : api['id']}

for api_name, api_data in apis.iteritems():
    api_data['resources'] = []

    resp = api_client.get_resources(restApiId=api_data['id'])

    for resource in resp['items']:
        methods = []
        if 'resourceMethods' in resource:
            methods = resource['resourceMethods'].keys()

        method_mapping = {}
        for method in methods:
            method_resp = api_client.get_method(restApiId=api_data['id'],
                                                resourceId=resource['id'],
                                                httpMethod=method
                                               )
            lambda_arn = method_resp['methodIntegration']['uri'].split('/')[-2]
            method_mapping[method] = lambda_arn

        api_data['resources'].append({
            'id' : resource['id'],
            'path' : resource['path'],
            'methods' : method_mapping
        })

api_lambda_mapping = []

for api in apis:
    graph.append("node [shape=record];")

    resources = []
    for resource in apis[api]['resources']:
        # graphviz doesn't like braces
        resource['path'] = resource['path'].replace('{', '(')
        resource['path'] = resource['path'].replace('}', ')')

        methods = '{'
        methods +=' | '.join(['<{}{}{}>{}'.format(api,
                                                  resource['path'],
                                                  method,
                                                  method
                                                 ) for method in resource['methods']
                             ])
        methods += '}'

        resources.append('{} | {}'.format(resource['path'], methods))

        for method in resource['methods']:
            api_lambda_mapping.append(['{}:<{}{}{}>'.format(api, api, resource['path'], method),
                                       ':'.join(resource['methods'][method].split(':')[6:])
                                      ])

    graph.append('"{}" [label="{} |{}"];'.format(api, api, '|'.join(resources)))


## SNS #########################################################################

sns_client = boto3.client('sns')

resp = sns_client.list_topics()

topics = {}
topic_arns = resp['Topics']

for topic in topic_arns:
    resp = sns_client.list_subscriptions_by_topic(TopicArn=topic['TopicArn'])

    topic_name = ':'.join(topic['TopicArn'].split(':')[5:])

    topics[topic_name] = []

    for sub in resp['Subscriptions']:
        endpoint = sub['Endpoint']
        if sub['Protocol'] == 'lambda':
            endpoint = ':'.join(sub['Endpoint'].split(':')[6:])
        topics[topic_name].append({'protocol' : sub['Protocol'], 'endpoint' : endpoint})



for topic in topics:
    graph.append(build_node(topic, 'topic'))


    for sub in topics[topic]:
        protocol = sub['protocol']

        graph.append(build_node(sub['endpoint'], protocol))

for topic in topics:
    for sub in topics[topic]:
        graph.append('"{}" -> "{}";'.format(topic, sub['endpoint']))



for mapping in api_lambda_mapping:
    graph.append(build_node(mapping[1], 'lambda'))

    graph.append('{} -> "{}"'.format(mapping[0], mapping[1]))



## LAMBDA ######################################################################

lambda_client = boto3.client('lambda')


# gather all lambda functions
function_list = lambda_client.list_functions()

for function in function_list['Functions']:
    func_name = function['FunctionName']

    # only grab the DEV alias versions for now
    try:
        resp = lambda_client.get_function(
            FunctionName=func_name,
            Qualifier='DEV'
        )
    except Exception, e: # no DEV version found
        continue

    # add ':DEV' since we're only grabbing DEV aliases for now
    graph.append(build_node(func_name + ':DEV', 'lambda'))

    # retrieve zipped code from provided location
    code_data = urllib2.urlopen(resp['Code']['Location'])
    zip_file = BytesIO()
    zip_file.write(code_data.read())
    zipped_file = ZipFile(zip_file)

    # Extremely slapdash solution for handling python and node
    if 'python' in function['Runtime']:
        # assume python lambda is only one file
        code = zipped_file.read(zipped_file.namelist()[0])

        # assume all SNS topic variables end in _TOPIC and are full ARNs
        topics = re.search(r".*?_TOPIC = 'arn:(.*?)'", code)
        if topics:
            for topic in topics.groups():
                graph.append(build_node(topic.split(':')[1], 'topic'))
                graph.append('"{}" -> "{}"'.format(func_name + ':DEV',
                                                   topic.split(':')[-1]))

        # ridiculous assumption of how dynamodb tables are called
        tables = re.search(r"dynamodb\.Table\('(.*?)'", code)
        if tables:
            for table in tables.groups():
                # assume DEV stage for python
                graph.append(build_node(table + 'DEV', 'dynamodb'))

                # assume dynamodb read/write
                # lambda -> table
                graph.append('"{}" -> "{}"'.format(func_name + ':DEV',
                                                   table + 'DEV'))
                # table -> lambda
                graph.append('"{}" -> "{}"'.format(table + 'DEV',
                                                   func_name + ':DEV'))
    else:
        # assume code is in index.js
        code = zipped_file.read('index.js')

        # assume Saws is used for topics
        topics = re.search(r"Saws.Topic\('(.*?)'", code)
        if topics:
            for topic in topics.groups():
                graph.append(build_node(topic + '-development', 'topic'))

                graph.append('"{}" -> "{}"'.format(func_name + ':DEV',
                                                   topic + '-development'))

        # ridiculous assumption of how dynamodb tables are called
        tables = re.search(r'TableName: "(.*?)"', code)
        if tables:
            for table in tables.groups():
                # assume -development stage for node
                graph.append(build_node(table + '-development', 'dynamodb'))

                # assume dynamodb read/write
                # lambda -> table
                graph.append('"{}" -> "{}"'.format(func_name + ':DEV',
                                                   table + '-development'))
                # table -> lambda
                graph.append('"{}" -> "{}"'.format(table + '-development',
                                                   func_name + ':DEV'))


graph.append("}")
print "\n".join(graph)
