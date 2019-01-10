import os,sys
import uuid

from rdflib import Namespace
from rdflib.namespace import XSD
from rdflib.resource import Resource
import types
import graphviz
from rdflib import Graph, RDF, URIRef, util, term
from rdflib.namespace import split_uri
import validators
import prov.model as pm
from urllib.request import urlretrieve, urlopen, URLError
from urllib.parse import quote
import requests
from fuzzywuzzy import fuzz
import json
from github import Github, GithubException
import getpass

#NIDM imports
from ..core import Constants
from .Project import Project
from .Session import Session
from .Acquisition import Acquisition
from .MRAcquisition import MRAcquisition
from .AcquisitionObject import AcquisitionObject
from .AssessmentAcquisition import AssessmentAcquisition
from .AssessmentObject import AssessmentObject
from .DemographicsObject import DemographicsObject
from .MRObject import MRObject
import logging





def read_nidm(nidmDoc):
    """
        Loads nidmDoc file into NIDM-Experiment structures and returns objects

        :nidmDoc: a valid RDF NIDM-experiment document (deserialization formats supported by RDFLib)

        :return: NIDM Project

    """

    from ..experiment.Project import Project
    from ..experiment.Session import Session


    #read RDF file into temporary graph
    rdf_graph = Graph()
    rdf_graph_parse = rdf_graph.parse(nidmDoc,format=util.guess_format(nidmDoc))


    #Query graph for project metadata and create project level objects
    #Get subject URI for project
    proj_id=None
    for s in rdf_graph_parse.subjects(predicate=RDF.type,object=URIRef(Constants.NIDM_PROJECT.uri)):
        #print(s)
        proj_id=s

    if proj_id is None:
        print("Error reading NIDM-Exp Document %s, Must have Project Object" % nidmDoc)
        exit(1)

    #Split subject URI into namespace, term
    nm,project_uuid = split_uri(proj_id)

    #print("project uuid=%s" %project_uuid)

    #create empty prov graph
    project = Project(empty_graph=True,uuid=project_uuid)

    #add namespaces to prov graph
    for name, namespace in rdf_graph_parse.namespaces():
        #skip these default namespaces in prov Document
        if (name != 'prov') and (name != 'xsd') and (name != 'nidm'):
            project.graph.add_namespace(name, namespace)

    #Cycle through Project metadata adding to prov graph
    add_metadata_for_subject (rdf_graph_parse,proj_id,project.graph.namespaces,project)


    #Query graph for sessions, instantiate session objects, and add to project._session list
    #Get subject URI for sessions
    for s in rdf_graph_parse.subjects(predicate=RDF.type,object=URIRef(Constants.NIDM_SESSION.uri)):
        #print("session: %s" % s)

        #Split subject URI for session into namespace, uuid
        nm,session_uuid = split_uri(s)

        #print("session uuid= %s" %session_uuid)

        #instantiate session with this uuid
        session = Session(project=project, uuid=session_uuid)

        #add session to project
        project.add_sessions(session)


        #now get remaining metadata in session object and add to session
        #Cycle through Session metadata adding to prov graph
        add_metadata_for_subject (rdf_graph_parse,s,project.graph.namespaces,session)

        #Query graph for acquistions dct:isPartOf the session
        for acq in rdf_graph_parse.subjects(predicate=Constants.DCT['isPartOf'],object=s):
            #Split subject URI for session into namespace, uuid
            nm,acq_uuid = split_uri(acq)
            #print("acquisition uuid: %s" %acq_uuid)

            #query for whether this is an AssessmentAcquisition of other Acquisition, etc.
            for rdf_type in  rdf_graph_parse.objects(subject=acq, predicate=RDF.type):
                #if this is an acquisition activity, which kind?
                if str(rdf_type) == Constants.NIDM_ACQUISITION_ACTIVITY.uri:
                    #if this is an MR acquisition then it's generated entity will have a predicate
                    # nidm:AcquisitionModality whose value is nidm:MagneticResonanceImaging
                    #first find the entity generated by this acquisition activity
                    for acq_obj in rdf_graph_parse.subjects(predicate=Constants.PROV["wasGeneratedBy"],object=acq):
                        #Split subject URI for session into namespace, uuid
                        nm,acq_obj_uuid = split_uri(acq_obj)
                        #print("acquisition object uuid: %s" %acq_obj_uuid)

                        #query for whether this is an MRI acquisition by way of looking at the generated entity and determining
                        #if it has the tuple [uuid Constants.NIDM_ACQUISITION_MODALITY Constants.NIDM_MRI]
                        if (acq_obj,URIRef(Constants.NIDM_ACQUISITION_MODALITY._uri),URIRef(Constants.NIDM_MRI._uri)) in rdf_graph:

                            #check whether this acquisition activity has already been instantiated (maybe if there are multiple acquisition
                            #entities prov:wasGeneratedBy the acquisition
                            if not session.acquisition_exist(acq_uuid):
                                acquisition=MRAcquisition(session=session,uuid=acq_uuid)
                                session.add_acquisition(acquisition)
                                #Cycle through remaining metadata for acquisition activity and add attributes
                                add_metadata_for_subject (rdf_graph_parse,acq,project.graph.namespaces,acquisition)


                            #and add acquisition object
                            acquisition_obj=MRObject(acquisition=acquisition,uuid=acq_obj_uuid)
                            acquisition.add_acquisition_object(acquisition_obj)
                            #Cycle through remaining metadata for acquisition entity and add attributes
                            add_metadata_for_subject(rdf_graph_parse,acq_obj,project.graph.namespaces,acquisition_obj)

                            #MRI acquisitions may have an associated stimulus file so let's see if there is an entity
                            #prov:wasAttributedTo this acquisition_obj
                            for assoc_acq in rdf_graph_parse.subjects(predicate=Constants.PROV["wasAttributedTo"],object=acq_obj):
                                #get rdf:type of this entity and check if it's a nidm:StimulusResponseFile or not
                                #if rdf_graph_parse.triples((assoc_acq, RDF.type, URIRef("http://purl.org/nidash/nidm#StimulusResponseFile"))):
                                if (assoc_acq,RDF.type,URIRef(Constants.NIDM_MRI_BOLD_EVENTS._uri)) in rdf_graph:
                                    #Split subject URI for associated acquisition entity for nidm:StimulusResponseFile into namespace, uuid
                                    nm,assoc_acq_uuid = split_uri(assoc_acq)
                                    #print("associated acquisition object (stimulus file) uuid: %s" % assoc_acq_uuid)
                                    #if so then add this entity and associate it with acquisition activity and MRI entity
                                    events_obj = AcquisitionObject(acquisition=acquisition,uuid=assoc_acq_uuid)
                                    #link it to appropriate MR acquisition entity
                                    events_obj.wasAttributedTo(acquisition_obj)
                                    #cycle through rest of metadata
                                    add_metadata_for_subject(rdf_graph_parse,assoc_acq,project.graph.namespaces,events_obj)



                        #query whether this is an assessment acquisition by way of looking at the generated entity and determining
                        #if it has the rdf:type "nidm:assessment-instrument"
                        #for acq_modality in rdf_graph_parse.objects(subject=acq_obj,predicate=RDF.type):
                        if (acq_obj, RDF.type, URIRef(Constants.NIDM_ASSESSMENT_ENTITY._uri)) in rdf_graph:

                            #if str(acq_modality) == Constants.NIDM_ASSESSMENT_ENTITY._uri:
                            acquisition=AssessmentAcquisition(session=session,uuid=acq_uuid)
                            if not session.acquisition_exist(acq_uuid):
                                session.add_acquisition(acquisition)
                                 #Cycle through remaining metadata for acquisition activity and add attributes
                                add_metadata_for_subject (rdf_graph_parse,acq,project.graph.namespaces,acquisition)

                            #and add acquisition object
                            acquisition_obj=AssessmentObject(acquisition=acquisition,uuid=acq_obj_uuid)
                            acquisition.add_acquisition_object(acquisition_obj)
                            #Cycle through remaining metadata for acquisition entity and add attributes
                            add_metadata_for_subject(rdf_graph_parse,acq_obj,project.graph.namespaces,acquisition_obj)
                        elif (acq_obj, RDF.type, URIRef(Constants.NIDM_MRI_BOLD_EVENTS._uri)) in rdf_graph:
                            #If this is a stimulus response file
                            #elif str(acq_modality) == Constants.NIDM_MRI_BOLD_EVENTS:
                            acquisition=Acquisition(session=session,uuid=acq_uuid)
                            if not session.acquisition_exist(acq_uuid):
                                session.add_acquisition(acquisition)
                                #Cycle through remaining metadata for acquisition activity and add attributes
                                add_metadata_for_subject (rdf_graph_parse,acq,project.graph.namespaces,acquisition)

                            #and add acquisition object
                            acquisition_obj=AcquisitionObject(acquisition=acquisition,uuid=acq_obj_uuid)
                            acquisition.add_acquisition_object(acquisition_obj)
                            #Cycle through remaining metadata for acquisition entity and add attributes
                            add_metadata_for_subject(rdf_graph_parse,acq_obj,project.graph.namespaces,acquisition_obj)



                #This skips rdf_type PROV['Activity']
                else:
                    continue



    return(project)


def get_RDFliteral_type(rdf_literal):
    if (rdf_literal.datatype == XSD["int"]):
        return (int(rdf_literal))
    elif ((rdf_literal.datatype == XSD["float"]) or (rdf_literal.datatype == XSD["double"])):
        return(float(rdf_literal))
    else:
        return (str(rdf_literal))

def add_metadata_for_subject (rdf_graph,subject_uri,namespaces,nidm_obj):
    """
    Cycles through triples for a particular subject and adds them to the nidm_obj

    :param rdf_graph: RDF graph object
    :param subject_uri: URI of subject to query for additional metadata
    :param namespaces: Namespaces in NIDM document
    :param nidm_obj: NIDM object to add metadata
    :return: None

    """
    #Cycle through remaining metadata and add attributes
    for predicate, objects in rdf_graph.predicate_objects(subject=subject_uri):
        #if find qualified association
        if predicate == URIRef(Constants.PROV['qualifiedAssociation']):
            #need to get associated prov:Agent uri, add person information to graph
            for agent in rdf_graph.objects(subject=subject_uri, predicate=Constants.PROV['wasAssociatedWith']):
                #add person to graph and also add all metadata
                person = nidm_obj.add_person(uuid=agent)
                #now add metadata for person
                add_metadata_for_subject(rdf_graph=rdf_graph,subject_uri=agent,namespaces=namespaces,nidm_obj=person)

            #get role information
            for bnode in rdf_graph.objects(subject=subject_uri,predicate=Constants.PROV['qualifiedAssociation']):
                #for bnode, query for object which is role?  How?
                #term.BNode.__dict__()

                #create temporary resource for this bnode
                r = Resource(rdf_graph,bnode)
                #get the object for this bnode with predicate Constants.PROV['hadRole']
                for r_obj in r.objects(predicate=Constants.PROV['hadRole']):
                    #create qualified names for objects
                    obj_nm,obj_term = split_uri(r_obj._identifier)
                    for uris in namespaces:
                        if uris.uri == URIRef(obj_nm):
                            #create qualified association in graph
                            nidm_obj.add_qualified_association(person=person,role=pm.QualifiedName(uris,obj_term))

        else:
            if validators.url(objects):
                #create qualified names for objects
                obj_nm,obj_term = split_uri(objects)
                for uris in namespaces:
                    if uris.uri == URIRef(obj_nm):
                        #prefix = uris.prefix
                        nidm_obj.add_attributes({predicate : pm.QualifiedName(uris,obj_term)})
            else:

                nidm_obj.add_attributes({predicate : get_RDFliteral_type(objects)})


def QuerySciCrunchElasticSearch(key,query_string,cde_only=False, anscestors=True):
    '''
    This function will perform an elastic search in SciCrunch on the [query_string] using API [key] and return the json package.
    :param key: API key from sci crunch
    :param query_string: arbitrary string to search for terms
    :param cde_only: default=False but if set will query CDE's only not CDE + more general terms...CDE is an instantiation of a term for
    a particular use.
    :return: json document of results form elastic search
    '''

    #Note, once Jeff Grethe, et al. give us the query to get the ReproNim "tagged" ancestors query we'd do that query first and replace
    #the "ancestors.ilx" parameter in the query data package below with new interlex IDs...
    #this allows interlex developers to dynamicall change the ancestor terms that are part of the ReproNim term trove and have this
    #query use that new information....


    #Add check for internet connnection, if not then skip this query...return empty dictionary


    headers = {
        'Content-Type': 'application/json',
    }

    params = (
        ('key', key),
    )
    if cde_only:
        if anscestors:
            data = '\n{\n  "query": {\n    "bool": {\n       "must" : [\n       {  "term" : { "type" : "cde" } },\n       { "terms" : { "ancestors.ilx" : ["ilx_0115066" , "ilx_0103210", "ilx_0115072", "ilx_0115070"] } },\n       { "multi_match" : {\n         "query":    "%s", \n         "fields": [ "label", "definition" ] \n       } }\n]\n    }\n  }\n}\n' %query_string
        else:
            data = '\n{\n  "query": {\n    "bool": {\n       "must" : [\n       {  "term" : { "type" : "cde" } },\n             { "multi_match" : {\n         "query":    "%s", \n         "fields": [ "label", "definition" ] \n       } }\n]\n    }\n  }\n}\n' %query_string
    else:
        if anscestors:
            data = '\n{\n  "query": {\n    "bool": {\n       "must" : [\n       {  "terms" : { "type" : ["cde" , "term"] } },\n       { "terms" : { "ancestors.ilx" : ["ilx_0115066" , "ilx_0103210", "ilx_0115072", "ilx_0115070"] } },\n       { "multi_match" : {\n         "query":    "%s", \n         "fields": [ "label", "definition" ] \n       } }\n]\n    }\n  }\n}\n' %query_string
        else:
            data = '\n{\n  "query": {\n    "bool": {\n       "must" : [\n       {  "terms" : { "type" : ["cde" , "term"] } },\n              { "multi_match" : {\n         "query":    "%s", \n         "fields": [ "label", "definition" ] \n       } }\n]\n    }\n  }\n}\n' %query_string

    response = requests.post('https://scicrunch.org/api/1/elastic-ilx/interlex/term/_search#', headers=headers, params=params, data=data)

    return json.loads(response.text)

def GetNIDMTermsFromSciCrunch(key,query_string,cde_only=False, ancestor=True):
    '''
    Helper function which issues elastic search query of SciCrunch using QuerySciCrunchElasticSearch function and returns terms list
    with label, definition, and preferred URLs in dictionary
    :param key: API key from sci crunch
    :param query_string: arbitrary string to search for terms
    :param cde_only: default=False but if set will query CDE's only not CDE + more general terms...CDE is an instantiation of a term for
    a particular use.
    :param ancestor: Boolean flag to tell Interlex elastic search to use ancestors (i.e. tagged terms) or not
    :return: dictionary with keys 'ilx','label','definition','preferred_url'
    '''

    json_data = QuerySciCrunchElasticSearch(key, query_string,cde_only,ancestor)
    results={}
    #check if query was successful
    if json_data['timed_out'] != True:
        #example printing term label, definition, and preferred URL
        for term in json_data['hits']['hits']:
            #find preferred URL
            results[term['_source']['ilx']] = {}
            for items in term['_source']['existing_ids']:
                if items['preferred']=='1':
                    results[term['_source']['ilx']]['preferred_url']=items['iri']
                results[term['_source']['ilx']]['label'] = term['_source']['label']
                results[term['_source']['ilx']]['definition'] = term['_source']['definition']

    return results

def load_nidm_owl_files():
    '''
    This function loads the NIDM-experiment related OWL files and imports, creates a union graph and returns it.
    :return: graph of all OWL files and imports from PyNIDM experiment
    '''
    #load nidm-experiment.owl file and all imports directly
    #create empty graph
    union_graph = Graph()
    #check if there is an internet connection, if so load directly from https://github.com/incf-nidash/nidm-specs/tree/master/nidm/nidm-experiment/terms and
    # https://github.com/incf-nidash/nidm-specs/tree/master/nidm/nidm-experiment/imports
    basepath=os.path.dirname(os.path.dirname(__file__))
    terms_path = os.path.join(basepath,"terms")
    imports_path=os.path.join(basepath,"terms","imports")

    imports=[
            "crypto_import.ttl",
            "dc_import.ttl",
            "iao_import.ttl",
            "nfo_import.ttl",
            "nlx_import.ttl",
            "obi_import.ttl",
            "ontoneurolog_instruments_import.ttl",
            "pato_import.ttl",
            "prv_import.ttl",
            "qibo_import.ttl",
            "sio_import.ttl",
            "stato_import.ttl"
    ]

    #load each import
    for resource in imports:
        temp_graph = Graph()
        try:

            temp_graph.parse(os.path.join(imports_path,resource),format="turtle")
            union_graph=union_graph+temp_graph

        except Exception:
            logging.info("Error opening %s import file..continuing" %os.path.join(imports_path,resource))
            continue

    owls=[
            "https://raw.githubusercontent.com/incf-nidash/nidm-specs/master/nidm/nidm-experiment/terms/nidm-experiment.owl"
    ]

    #load each owl file
    for resource in owls:
        temp_graph = Graph()
        try:
            temp_graph.parse(location=resource, format="turtle")
            union_graph=union_graph+temp_graph
        except Exception:
            logging.info("Error opening %s owl file..continuing" %os.path.join(terms_path,resource))
            continue


    return union_graph



def fuzzy_match_terms_from_graph(graph,query_string):
    '''
    This function performs a fuzzy match of the constants in Constants.py list nidm_experiment_terms for term constants matching the query....i
    ideally this should really be searching the OWL file when it's ready
    :param query_string: string to query
    :return: dictionary whose key is the NIDM constant and value is the match score to the query
    '''


    match_scores={}

    #search for labels rdfs:label and obo:IAO_0000115 (description) for each rdf:type owl:Class
    for term in graph.subjects(predicate=RDF.type, object=Constants.OWL["Class"]):
        for label in graph.objects(subject=term, predicate=Constants.RDFS['label']):
            match_scores[term] = {}
            match_scores[term]['score'] = fuzz.token_sort_ratio(query_string,label)
            match_scores[term]['label'] = label
            match_scores[term]['url'] = term
            match_scores[term]['definition']=None
            for description in graph.objects(subject=term,predicate=Constants.OBO["IAO_0000115"]):
                match_scores[term]['definition'] =description

    #for term in owl_graph.classes():
    #    print(term.get_properties())
    return match_scores


def authenticate_github(authed=None,credentials=None):
    '''
    This function will hangle GitHub authentication with or without a token.  If the parameter authed is defined the
    function will check whether it's an active/valide authentication object.  If not, and username/token is supplied then
    an authentication object will be created.  If username + token is not supplied then the user will be prompted to input
    the information.
    :param authed: Optional authenticaion object from PyGithub
    :param credentials: Optional GitHub credential list username,password or username,token
    :return: GitHub authentication object or None if unsuccessful

    '''

    print("GitHub authentication...")
    indx=1
    maxtry=5
    while indx < maxtry:
        if (len(credentials)>= 2):
            #authenticate with token
            g=Github(credentials[0],credentials[1])
        elif (len(credentials)==1):
            pw = getpass.getpass("Please enter your GitHub password: ")
            g=Github(credentials[0],pw)
        else:
            username = input("Please enter your GitHub user name: ")
            pw = getpass.getpass("Please enter your GitHub password: ")
            #try to logging into GitHub
            g=Github(username,pw)

        authed=g.get_user()
        try:
            #check we're logged in by checking that we can access the public repos list
            repo=authed.public_repos
            logging.info("Github authentication successful")
            new_term=False
            break
        except GithubException as e:
            logging.info("error logging into your github account, please try again...")
            indx=indx+1

    if (indx == maxtry):
        logging.critical("GitHub authentication failed.  Check your username / password / token and try again")
        return None
    else:
        return authed

def map_variables_to_terms(df,apikey,directory, output_file=None,json_file=None,github=None,owl_file=None):
    '''

    :param df: data frame with first row containing variable names
    :param json_file: optional json document with variable names as keys and minimal fields "definition","label","url"
    :param apikey: scicrunch key for rest API queries
    :param github: boolean flag, if set local term definitions will be added to GitHub
    :param owl_file: optional OWL file for additional terms
    :param output_file: output filename to save variable-> term mappings
    :param directory: if output_file parameter is set to None then use this directory to store default JSON mapping file if doing variable->term mappings
    :return:return dictionary mapping variable names (i.e. columns) to terms
    '''
    #minimum match score for fuzzy matching NIDM terms
    min_match_score=50

    #dictionary mapping column name to preferred term
    column_to_terms={}

    #flag for whether a new term has been defined, on first occurance ask for namespace URL
    new_term=True

    #check if user supplied a JSON file and we already know a mapping for this column
    if json_file != None:
        #load file and
        #json_map = json.load(open(json_file))
        with open(json_file,'r+') as f:
            json_map = json.load(f)
    #if no JSON mapping file was specified then create a default one if an apikey was specified (or github) because it
    #means the user is going to do some variable to term mappings and the JSON file will save the dictionary out for
    #reuse...or if the program crashes so you don't have to start over :)
    elif apikey or github:
        #create a json_file filename from the output file filename
        if not output_file:
            output_file = os.path.join(directory,"nidm_json_map.json")
        #remove ".ttl" extension
        else:
            output_file = os.path.join(os.path.dirname(output_file), os.path.splitext(os.path.basename(output_file))[0]+"_json_map.json")
        #with open(json_file, 'w') as f:
        #    json_map = json.dumps({})



    #Authenticate GitHub if user selected to use github
    if github != None:
        authed = authenticate_github(credentials=github)

    #iterate over columns
    for column in df.columns:
            #tk stuff
            #root=tk.Tk()
            #listb=NewListbox(root,selectmode=tk.SINGLE)
        #search term for elastic search
        search_term=str(column)
        #loop variable for terms markup
        go_loop=True
        #set up a dictionary entry for this column
        column_to_terms[column] = {}

        #if we loaded a json file with existing mappings
        try:
            json_map

            #check for column in json file
            if (json_map!= None) and (column in json_map):

                column_to_terms[column]['label'] = json_map[column]['label']
                column_to_terms[column]['definition'] = json_map[column]['definition']
                column_to_terms[column]['url'] = json_map[column]['url']

                print("Column %s already mapped to terms in user supplied JSON mapping file" %column)
                print("Label: %s" %column_to_terms[column]['label'])
                print("Definition: %s" %column_to_terms[column]['definition'])
                print("Url: %s" %column_to_terms[column]['url'])
                print("---------------------------------------------------------------------------------------")
                continue
        except NameError:
            print("json mapping file not supplied")
        #flag for whether to use ancestors in Interlex query or not
        ancestor=True

        #load NIDM OWL files if user requested it
        if owl_file:
            nidm_owl_graph = load_nidm_owl_files()

        #loop to find a term definition by iteratively searching scicrunch...or defining your own
        while go_loop:
            #variable for numbering options returned from elastic search
            option=1



            #for each column name, query Interlex for possible matches
            search_result = GetNIDMTermsFromSciCrunch(apikey,search_term,cde_only=True,ancestor=ancestor)

            temp=search_result.copy()
            print("Search Term: %s" %search_term)
            print("Search Results: ")
            for key,value in temp.items():

                print("%d: Label: %s \t Definition: %s \t Preferred URL: %s " %(option,search_result[key]['label'],search_result[key]['definition'],search_result[key]['preferred_url']  ))
                #add to dialog box for user to check which one is correct
                #listb.insert("end",search_result[key]['label']+", " +search_result[key]['definition'])
                search_result[str(option)] = key
                option=option+1

             #if user supplied an OWL file to search in for terms
            if owl_file:
                #Add existing NIDM Terms as possible selections which fuzzy match the search_term
                nidm_constants_query = fuzzy_match_terms_from_graph(nidm_owl_graph, search_term)
                #nidm_constants_query = sorted(nidm_constants_query_unsorted.items(),key=operator.itemgetter(1))

                for key, subdict in nidm_constants_query.items():
                    if nidm_constants_query[key]['score'] > min_match_score:
                        print("%d: Label(NIDM Term): %s \t Definition: %s \t URL: %s" %(option, nidm_constants_query[key]['label'], nidm_constants_query[key]['definition'], nidm_constants_query[key]['url']))
                        search_result[key] = {}
                        search_result[key]['label']=nidm_constants_query[key]['label']
                        search_result[key]['definition']=nidm_constants_query[key]['definition']
                        search_result[key]['preferred_url']=nidm_constants_query[key]['url']
                        search_result[str(option)] = key
                        option=option+1
            #else just give a list of the NIDM constants for user to choose
            else:
                match_scores={}
                for index,item in enumerate(Constants.nidm_experiment_terms):
                    match_scores[item._str] = fuzz.ratio(search_term,item._str)
                match_scores_sorted=sorted(match_scores.items(), key=lambda x: x[1])
                for score in match_scores_sorted:
                    if ( score[1] > min_match_score):
                        for term in Constants.nidm_experiment_terms:
                            if term._str==score[0]:
                                search_result[term._str] = {}
                                search_result[term._str]['label']=score[0]
                                search_result[term._str]['definition']=score[0]
                                search_result[term._str]['preferred_url']=term._uri
                                search_result[str(option)] = term._str
                                print("%d: NIDM Constant: %s \t URI: %s" %(option,score[0],term._uri))
                                option=option+1



            if ancestor:
                #Broaden Interlex search
                print("%d: Broaden Interlex query " %option)
            else:
                #Narrow Interlex search
                print("%d: Narrow Interlex query " %option)
            option=option+1


            #Add option to change query string
            print("%d: Change Interlex query string from: \"%s\"" %(option,column))
            if github is not None:
                option=option+1
                #Add option to define your own term
                print("%d: Define my own term for this variable" %option)

            print("---------------------------------------------------------------------------------------")
            #Wait for user input
            selection=input("Please select an option (1:%d) from above: \t" %(option))

            #Make sure user selected one of the options.  If not present user with selection input again
            while (not selection.isdigit()):
                #Wait for user input
                selection=input("Please select an option (1:%d) from above: \t" %(option))


            #toggle use of ancestors in interlext query or not
            if int(selection) == (option-2):
                ancestor=not ancestor
            #check if selection is to re-run query with new search term
            elif int(selection) == (option-1):
                #ask user for new search string
                search_term = input("Please input new search term for CSV column: %s \t:" % column)
                print("---------------------------------------------------------------------------------------")

            elif int(selection) == option:
                #user wants to define their own term.  Ask for term label and definition
                print("\nYou selected to enter a new term for CSV column: %s" % column)
                if (new_term):
                    #checking to see if user set command line flag -github to use github for local terms
                    if github != None:
                        #test if authed object is still active
                        try:
                            #check we're logged in by checking that we can access the public repos list
                            repo=authed.public_repos
                        except GithubException as e:
                            print("error logging into your github account, please try again...")
                            authed=authenticate_github(authed=authed,credentials=github)

                        #print("You've selected using GitHub to store your locally defined terms.")
                        #while True:
                        #    user = input("Please enter your GitHub user name: ")
                        #    pw = getpass.getpass("Please enter your GitHub password: ")
                        #    print("\nLogging into GitHub...")
                        #    #try to logging into GitHub
                        #    g=Github(user,pw)
                        #    authed=g.get_user()
                        #    try:
                        #        #check we're logged in by checking that we can access the public repos list
                        #        repo=authed.public_repos
                        #        print("Success!")
                        #        new_term=False
                        #        break
                        #    except GithubException as e:
                        #        print("error logging into your github account, please try again...")

                        #check to see if nidm-local-terms repo exists
                        try:
                            repo=authed.get_repo('nidm-local-terms')
                            #set namespace to repo URL
                            local_namespace = repo.html_url
                            print("\nnidm-local-terms repo already exists, continuing...\n")
                        except GithubException as e:
                            print("\nnidm-local-terms repo doesn't exist, creating...\n")
                            #try to create the repo
                            try:
                                repo=authed.create_repo(name='nidm-local-terms',description='Created for NIDM document local term definitions')
                            except GithubException as e:
                                print("Unable to create terms repo, exception: %s" % e)
                                exit()
                    else:
                        ####THIS PART NEEDS WORK....WHAT TO DO IF USER DID NOT WANT TO DEFINE LOCAL TERMS ON GITHUB?####
                        local_namespace=" "
                        while not validators.url(local_namespace):
                            print("By not setting the command line flag \"-github\" you have selected to store locally defined terms in a sidecar RDF file on disk")
                            local_namespace = input("Please enter a valid URL for your namespace (e.g. http://nidm.nidash.org): ")
                        #if we're out of loop then namespace was valid so change new_term variable to prevent
                        new_term=False

                #collect term information from user
                term_label = input("Please enter a term label for this column (%s):\t" % column)
                if term_label == '':
                    term_label = column
                term_definition = input("Please enter a definition:\t")
                term_units = input("Please enter the units:\t")
                term_datatype = input("Please enter the datatype:\t")
                term_min = input("Please enter the minimum value:\t")
                term_max = input("Please enter the maximum value:\t")
                term_variable_name = column


                #don't need to continue while loop because we've defined a term for this CSV column
                go_loop=False

                #if we're using Github
                if authed:
                    #add term as issue
                    body = "Label/Name: " + term_label + "\nDefinition/Description: " + term_definition + "\nUnits: " + \
                        term_units + "\nDatatype/Value Type: " + term_datatype + "\nMinimum Value: " + term_min + \
                        "\nMaximum Value: " + term_max + "\nVariable Name: " + term_variable_name

                    try:
                        issue=repo.create_issue(title=term_label, body=body)
                        #add inputted term to column_to_term mapping dictionary
                        column_to_terms[column]['label'] = term_label
                        column_to_terms[column]['definition'] = term_definition
                        column_to_terms[column]['url'] = issue.html_url

                    except GithubException as e:
                        print("error creating issue...\n")
                        #try to create the repo




                #print mappings
                print("Stored mapping Column: %s ->  ")
                print("Label: %s" %column_to_terms[column]['label'])
                print("Definition: %s" %column_to_terms[column]['definition'])
                print("Url: %s" %column_to_terms[column]['url'])
                print("---------------------------------------------------------------------------------------")


            else:
                #add selected term to map
                column_to_terms[column]['label'] = search_result[search_result[selection]]['label']
                column_to_terms[column]['definition'] = search_result[search_result[selection]]['definition']
                column_to_terms[column]['url'] = search_result[search_result[selection]]['preferred_url']

                #print mappings
                print("Stored mapping Column: %s ->  " % column)
                print("Label: %s" %column_to_terms[column]['label'])
                print("Definition: %s" %column_to_terms[column]['definition'])
                print("Url: %s" %column_to_terms[column]['url'])
                print("---------------------------------------------------------------------------------------")

                #don't need to continue while loop because we've defined a term for this CSV column
                go_loop=False

         #write variable-> terms map as JSON file to disk
        #get -out directory from command line parameter
        if output_file!= None:
            #dir = os.path.dirname(output_file)
            #file_path=os.path.relpath(output_file)

            with open(output_file,'w+') as fp:
                json.dump(column_to_terms,fp)



        #listb.pack()
        #listb.autowidth()
        #root.mainloop()
        #input("Press Enter to continue...")

    return column_to_terms
