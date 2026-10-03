from copy import deepcopy
import json
import sys
from uuid import uuid4

from .contracts import AOSFault, ErrorCode, Option, Prediction, State, digest
from .decision import decision_request
from .scientist_evidence_client import _failure_locations
from .scientist_protocol import ScientistTurnRequest
from .scientist_transport import ScientistAdmissionError


def _deny_state(state, options):
    raise ScientistAdmissionError('Current typed decision/task authority is not configured')


class ScientistDecisionEngine:
    def __init__(self, pins: dict, client, *, verify_state=_deny_state):
        self.pins = deepcopy(pins)
        self.client = client
        self.verify_state = verify_state
        self._deployment_digest = digest(self.pins)
        self.identity = {'deployment_id': 'decider-' + self._deployment_digest,
                         'kind': 'scientist_decider_broker', 'real_model': True, 'pins': deepcopy(self.pins)}
        self.last_metrics = {}
        self.last_request = None
        self.last_response = None
        self.before_decision_dispatch = None

    def _verify(self, state, options):
        if (state.owner != 'AGENT' or state.deployment_id != 'decider-' + self._deployment_digest
                or digest(self.pins) != self._deployment_digest):
            raise ScientistAdmissionError('Decision owner or pinned deployment differs')
        if self.verify_state(state.model_copy(deep=True), [option.model_copy(deep=True) for option in options]) is not None:
            raise ScientistAdmissionError('Typed task authority verifier must complete or raise')

    async def decide(self, state: State, options: list[Option]) -> Prediction:
        try:
            return await self._decide(state, options)
        except BaseException as error:
            try:
                print(json.dumps({'boundary': 'scientist_decision',
                                  'locations': _failure_locations(error)}, sort_keys=True),
                      file=sys.stderr, flush=True)
            except Exception:
                pass
            raise

    async def _decide(self, state: State, options: list[Option]) -> Prediction:
        state = State.model_validate(state.model_dump(), strict=True)
        options = [Option.model_validate(option.model_dump(), strict=True) for option in options]
        self.last_metrics, self.last_request, self.last_response = {}, None, None
        self._verify(state, options)
        if (not 2 <= len(options) <= 10 or len({option.id for option in options}) != len(options)
                or len({option.label for option in options}) != len(options)
                or any(not option.id.strip() or not option.label.strip() for option in options)):
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Invalid bounded decision options')
        payload = decision_request(state, options)
        request = ScientistTurnRequest(request_id=uuid4().hex, profile_id='aos.decider.turn.v1',
            deployment_digest=self._deployment_digest, payload={'request': payload})
        if self.before_decision_dispatch is not None:
            self.before_decision_dispatch(deepcopy(payload))
        self._verify(state, options)
        self.last_request = deepcopy(payload)
        receipt = await self.client.infer(request)
        self._verify(state, options)
        try:
            response = receipt.response
            if (receipt.request_id != request.request_id or receipt.profile_id != request.profile_id
                    or receipt.deployment_digest != request.deployment_digest
                    or response['deployment_digest'] != request.deployment_digest
                    or not isinstance(response['metrics'], dict)):
                raise ValueError('Decision receipt correlation differs')
            prediction = Prediction.model_validate(response['prediction'], strict=True)
            prediction.validate_options(options)
            self.last_metrics = deepcopy(response['metrics'])
            self.last_response = prediction.model_dump(mode='json')
            return prediction
        except (ValueError, KeyError, TypeError):
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Invalid broker-backed Decider response') from None
