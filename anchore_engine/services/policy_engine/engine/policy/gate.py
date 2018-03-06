"""
Base types for gate implementations and triggers.

"""
import copy
import hashlib
import inspect

import anchore_engine
from anchore_engine.subsys import logger
from anchore_engine.services.policy_engine.engine.policy.params import TriggerParameter
from anchore_engine.services.policy_engine.engine.policy.exceptions import ParameterValueInvalidError, InvalidParameterError,  \
    TriggerEvaluationError, PolicyRuleValidationErrorCollection, ValidationError


class GateMeta(type):
    """
    Metaclass to create a registry for all subclasses of Gate for finding, building, and documenting the gates.
    
    """
    def __init__(cls, name, bases, dct):
        if not hasattr(cls, 'registry'):
            cls.registry = {}
        else:
            if '__gate_name__' in dct:
                gate_id = dct['__gate_name__'].lower()
                cls.registry[gate_id] = cls

        super(GateMeta, cls).__init__(name, bases, dct)

    def get_gate_by_name(cls, name):
        return cls.registry[name.lower()]

    def registered_gate_names(cls):
        return cls.registry.keys()


class ExecutionContext(object):
    """
    Execution context defines the gate execution environment including logging, db connections, and cache space.
    The context is configured and set for each gate invocation.
    
    """

    def __init__(self, db_session, configuration, **params):
        self.db = db_session
        self.configuration = configuration
        self.params = params
        self.data = {}


class TriggerMatch(object):
    """
    An instance of a fired trigger
    """

    def __init__(self, trigger, match_instance_id=None, msg=None):
        self.trigger = trigger
        self.id = match_instance_id
        self.msg = msg

        # Compute a hash-based trigger_id for matching purposes (this is legacy from Anchore CLI)
        if not self.id:
            gate_id = self.trigger.gate_cls.__gate_name__
            self.id = hashlib.md5(''.join([gate_id, self.trigger.__trigger_name__, self.msg])).hexdigest()

    def json(self):
        return {
            'trigger': self.trigger.__trigger_name__,
            'trigger_id': self.id,
            'message': self.msg
        }

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return '<{}.{} Trigger:{}, Id: {}, Msg: {}>'.format(self.__class__.__module__, self.__class__.__name__, self.trigger.__trigger_name__, self.id, self.msg)


class BaseTrigger(object):
    """
    An evaluation trigger, representing something found image analysis specifically requested. Contained
    by a single gate, with execution context defined by the parent gate object.

    To define parameters for the trigger, simply define attribtes of the class that are of type (or subclass) TriggerParameter.
    Upon instantiation the trigger object will have instance-attributes of the same name as the class attributes but with the provided
    parameter values as the object value.

    e.g. in class definition:

    testparam = TriggerParamter(display_name='should_fire', is_required=False, validator=BooleanValidator())

    in usage of the instance object:

    self.testparam  is the realized value of the parameter
    self.__class__.testparam is the TriggerParameter object that defines



    """

    __trigger_name__ = None  # The base name of the trigger
    __description__ = None  # The test description of the trigger for users.
    __msg__ = None  # Default message if not defined for specific trigger instance
    __trigger_id__ = None  # If trigger has a specific id, set here, else it is calculated at evaluation time

    def __init__(self, parent_gate_cls, rule_id=None, **kwargs):
        """
        Instantiate the trigger with a specific set of parameters. Does not evaluate the trigger, just configures
        it for execution.
        """
        self.gate_cls = parent_gate_cls
        self.msg = None
        self.eval_params = {}
        self._fired_instances = []
        self.rule_id = rule_id

        # Setup the parameters, try setting each. If not provided, set to None to handle validation path for required params
        invalid_params = []

        # The list of class vars that are parameters
        params = self.__class__._parameters()

        if kwargs is None:
            kwargs = {}

        # Find all class objects that are params
        for attr_name, param_obj in params.items():
            try:
                setattr(self, attr_name, copy.deepcopy(param_obj))
                getattr(self, attr_name).set_value(kwargs.get(param_obj.name, None))
            except ValidationError as e:
                invalid_params.append(ParameterValueInvalidError(validation_error=e, gate=self.gate_cls.__gate_name__, trigger=self.__trigger_name__, rule_id=self.rule_id))

        # Then, check for any parameters provided that are not defined in the trigger.
        if kwargs:
            given_param_names = set(kwargs.keys())
            for i in given_param_names.difference(set([x.name for x in params.values()])):
                # Need to aggregate and return all invalid if there is more than one
                invalid_params.append(InvalidParameterError(i, params.keys(), trigger=self.__trigger_name__, gate=self.gate_cls.__gate_name__))

        if invalid_params:
            raise PolicyRuleValidationErrorCollection(invalid_params, trigger=self.__trigger_name__, gate=self.gate_cls.__gate_name__)

    @classmethod
    def _parameters(cls):
        """
        Returns a dict containing the class attribute name-to-object mapping in this class definition.

        :return: dict of (name -> obj) tuples enumerating all TriggerParameter objects defined for this class
        """

        return {x.name: x.object for x in filter(lambda attr: attr.kind == 'data' and isinstance(attr.object, anchore_engine.services.policy_engine.engine.policy.params.TriggerParameter), inspect.classify_class_attrs(cls))}

    def parameters(self):
        """
        Returns a map of display names of the TriggerParameters defined for this Trigger to values
        :return:
        """
        return {attr_name: getattr(self, attr_name) for attr_name in self._parameters().keys()}

    def legacy_str(self):
        """
        Returns a string in the format of the old anchore gate file outputs:
        <TRIGGER> <MSG>
        :return: str
        """
        return self.__trigger_name__ + ' ' + self.msg

    def execute(self, image_obj, context):
        """
        Main entry point for the trigger execution. Will clear any previously saved exec state and call the evaluate() function.
        :param image_obj:
        :param context:
        :return:
        """
        self.reset()
        try:
            self.evaluate(image_obj, context)
        except Exception as e:
            logger.exception('Error evaluating trigger. Aborting trigger execution')
            raise TriggerEvaluationError(trigger=self, message='Error executing gate {} trigger {} with params: {}. Msg: {}'.format(self.gate_cls.__gate_name__, self.__trigger_name__, self.eval_params, e.message))

        return True

    def evaluate(self, image_obj, context):
        """
        Evaluate against the image update the state of the trigger based on result.
        If a match/fire is found, this code should call self._fire(), which may be called for each occurrence of a condition
        match.

        Result is the population of self._fired_instances, which can be accessed via the 'fired' property
        """
        raise NotImplementedError()

    def _fire(self, instance_id=None, msg=None):
        """
        Internal function used by evaluation code to indicate a match found. May be called many times and results in
        a record added to the _fired_instances list

        :param instance_id: an id to associate with this specific firing. optional. e.g. CVE ID, filename, etc
        :param msg: A specific message (may be visible to users) for detail on the fired trigger
        :return: 
        """
        if not msg:
            msg = self.__msg__

        if not instance_id and self.__trigger_id__:
            instance_id = self.__trigger_id__

        self._fired_instances.append(TriggerMatch(self, match_instance_id=instance_id, msg=msg))

    @property
    def did_fire(self):
        return len(self._fired_instances) > 0

    @property
    def fired(self):
        return self._fired_instances

    def reset(self):
        """
        To be called between invocations with different images and contexts
        :return:
        """
        self._fired_instances = []

    def json(self):
        return {
            'name': self.__trigger_name__,
            'trigger_id': self.__trigger_id__,
            'params': self.parameters(),
            'fired': [f.json() for f in self.fired]
        }

    @classmethod
    def config_json(cls):
        return {
            'name': cls.__trigger_name__,
            'params': cls.parameters(),
            'id': cls.__trigger_id__
        }

    def __repr__(self):
        return '<{}.{} object Name:{}, TriggerId:{}, Params:{}>'.format(self.__class__.__module__, self.__class__.__name__, self.__trigger_name__, self.__trigger_id__, self.parameters() if self.parameters() else [])


class Gate(object):
    """
    Base type for a gate module.
    
    __gate_name__: The name to map to the policy item (e.g. DOCKERFILECHECK)
    
    To associate triggers with a gate, declare scoped classes within the Gate class. E.g.
    class MyGate(Gate):
       class MyTrigger(BaseTrigger):
         __trigger_base__ = 'MyTrigger1'
         __description__ = 'My testing trigger that fires for, like, no reason at all.'
         __params__ = {'Danger': bool, 'Zone': str}
         
    To ensure a gate is updated on data changes, configure __watches__ to include the list of WatchFilters for
    the entity classes that impact this gate. Example: AnchoreSec gate watches Vulnerabilities, and GemCheck watches AppliationPackages
    
    A gate is a collection of Triggers and a configured execution environment for each. It receives a basic execution
    context from the caller, but can customize it before executing each trigger. Generally a gate groups a set of triggers
    that use a common setup and execution context (e.g. docker file checks or vulnerability checks)
    
    The result of a gate evaluation is an ExecutionResult.
    
    """
    __metaclass__ = GateMeta

    __gate_name__ = None
    __triggers__ = []
    __deprecated_trigger_names__ = []
    __description__ = None

    @classmethod
    def has_trigger(cls, name):
        """
        Returns true if the given name is a valid trigger name for triggers associated with this Gate
        :param name: 
        :return: 
        """
        return any(map(lambda x: x.__trigger_name__.lower() == name.lower(), cls.__triggers__))

    @classmethod
    def trigger_names(cls):
        return [x.__trigger_name__.lower() for x in cls.__triggers__]

    @classmethod
    def is_deprecated_trigger(cls, name):
        if name is None:
            raise ValueError('Trigger name cannot be None')

        return name.lower() in cls.__deprecated_trigger_names__

    @classmethod
    def get_trigger_named(cls, name):
        """
        Returns an the trigger class with the specified name
        :param name: name to match against the trigger classes' __trigger_name__ value 
        :return: a trigger class object 
        """

        name = name.lower()

        found = filter(lambda x: x.__trigger_name__.lower() == name, cls.__triggers__)
        if found:
            return found[0]
        else:
            raise KeyError(name)

    def __init__(self):
        """
        Intialize the gate for execution with the specified context from C{ExecutionContext}.
        
        :param context: a context providing db connections, etc 
        """
        self.image = None
        self.selected_triggers = None
        self.evaluated_triggers = []
        self.evaluated_at = None
        self.evaluation_duration = None
        self.evaluation_success = False

    def prepare_context(self, image_obj, context):
        """
        Called immediately prior to gate execution, a hook to allow optimizations or prep of the context or image
        data prior to execution of the gate/triggers.
        :return:         
        """
        return context

    def json(self):
        """
        Return a json-dict of the gate definition
        :return: 
        """
        trigger_json = [t.config_json() for t in self.__triggers__]
        eval_json = [t.json() for t in self.evaluated_triggers] if self.evaluated_triggers else []

        return {
            'name': self.__gate_name__,
            'configured_triggers': trigger_json,
            'evaluation': {
                'triggers': eval_json,
                'timestamp': self.evaluated_at,
                'duration': self.evaluation_duration
            }
        }

    @classmethod
    def config_json(cls):
        """
        Return a json-dict of the gate definition
        :return: 
        """
        trigger_json = [t.json() for t in cls.__triggers__]
        return {
            'name': cls.__gate_name__,
            'triggers': trigger_json
        }

    def __repr__(self):
        return '<Gate {}>'.format(self.__gate_name__)
