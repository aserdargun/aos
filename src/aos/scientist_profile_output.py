from .contracts import canonical
from .scientist_admission_history import ScientistAdmissionHistory, ScientistOutputContractPin
from .scientist_bonsai_receipt import validate_bonsai_receipt
from .scientist_decider_receipt import validate_decider_receipt
from .scientist_protocol import scientist_request_sha256
from .scientist_transport import ScientistAdmissionError


class AdmissionProfileOutputValidator:
    def __init__(self, history, output_contract, *, context_tokens=16384):
        if not isinstance(history, ScientistAdmissionHistory):
            raise TypeError('Profile output validation requires original admission history')
        if history.record_version != '2.0':
            raise ScientistAdmissionError('Pinned profile output requires explicit admission record version2.0')
        if type(context_tokens) is not int or not 256 <= context_tokens <= 16384:
            raise ValueError('Profile output trusted context limit is invalid')
        expected = ScientistOutputContractPin.model_validate(output_contract, strict=True)
        self.history = history
        self.expected_json = canonical(expected.model_dump(mode='json'))
        self.context_tokens = context_tokens

    def verify_admission(self, request):
        record, checksum = self.history.read(request.request_id)
        binding = record.admission_binding
        observed = getattr(binding.profile_pin, 'output_contract', None)
        if (record.schema_version != '2.0'
                or record.request_sha256 != scientist_request_sha256(request)
                or binding.profile_id != request.profile_id
                or binding.profile_pin.deployment_digest != request.deployment_digest
                or observed is None
                or canonical(observed.model_dump(mode='json')) != self.expected_json):
            raise ScientistAdmissionError('Profile output pin differs from original admission')
        return checksum

    def __call__(self, request, receipt):
        checksum = self.verify_admission(request)
        if request.profile_id == 'aos.decider.turn.v1':
            validate_decider_receipt(request, receipt)
        else:
            validate_bonsai_receipt(request, receipt, context_tokens=self.context_tokens)
        if self.history.read(request.request_id)[1] != checksum:
            raise ScientistAdmissionError('Original admission changed during output validation')


def admission_profile_validator(history, output_contract, *, context_tokens=16384):
    return AdmissionProfileOutputValidator(history, output_contract, context_tokens=context_tokens)
