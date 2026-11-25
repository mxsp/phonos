"""Translation logic for converting data types between models."""
from __future__ import annotations

from typing import Any, Dict, Optional, Callable, List, Tuple
import logging

from .io_types import IOType, MODEL_SIGNATURES

logger = logging.getLogger("phonos.mrs.translator")

# Type alias for a translation function: (data, context) -> transformed_data
TranslatorFunc = Callable[[Any, Dict[str, Any]], Any]

class DataTranslator:
    def __init__(self):
        self._registry: Dict[Tuple[IOType, IOType], TranslatorFunc] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register default generic translations."""
        # Identity translation (Same type)
        for t in IOType:
            self.register(t, t, lambda x, ctx: x)

        # Example: PDB Text -> PDB File ID (Conceptual, would need DB access)
        # self.register(IOType.PDB_TEXT, IOType.PDB_FILE_ID, self._text_to_file_mock)

    def register(self, source_type: IOType, target_type: IOType, func: TranslatorFunc):
        """Register a new translation block."""
        self._registry[(source_type, target_type)] = func

    def find_path(self, available_outputs: Dict[str, Any], prev_model_type: str, next_model_type: str) -> Dict[str, Any]:
        """
        Determine the best way to map outputs from prev_model to inputs of next_model.
        
        Args:
            available_outputs: The data dictionary returned by the previous job.
            prev_model_type: The type of the model that just finished.
            next_model_type: The type of the model about to start.
            
        Returns:
            A dictionary of arguments to inject into the next job.
        """
        
        prev_sig = MODEL_SIGNATURES.get(prev_model_type)
        next_sig = MODEL_SIGNATURES.get(next_model_type)
        
        if not prev_sig or not next_sig:
            logger.warning(f"Missing signature for {prev_model_type} or {next_model_type}. Fallback to raw merge.")
            return {}

        injected_args = {}
        
        # Check all required inputs for the next model
        required_inputs = next_sig.get("inputs", {})
        optional_inputs = next_sig.get("optional_inputs", {})
        
        # Combine checks (prioritizing required)
        all_potential_inputs = {**optional_inputs, **required_inputs}
        
        for input_arg, input_type in all_potential_inputs.items():
            # Try to find a match in the previous outputs
            match_found = False
            
            for output_key, output_type in prev_sig.get("outputs", {}).items():
                if output_key not in available_outputs:
                    continue
                    
                # 1. Direct Type Match
                if output_type == input_type:
                    val = available_outputs[output_key]
                    injected_args[input_arg] = val
                    match_found = True
                    logger.info(f"Direct Match: {prev_model_type}.{output_key} -> {next_model_type}.{input_arg} ({input_type})")
                    break
                
                # 2. Check for Translation
                translator = self._registry.get((output_type, input_type))
                if translator:
                    val = available_outputs[output_key]
                    try:
                        translated_val = translator(val, {})
                        injected_args[input_arg] = translated_val
                        match_found = True
                        logger.info(f"Translation: {prev_model_type}.{output_key} -> {next_model_type}.{input_arg} via {translator.__name__}")
                        break
                    except Exception as e:
                        logger.error(f"Translation failed: {e}")
            
            if not match_found and input_arg in required_inputs:
                logger.warning(f"Could not resolve required input '{input_arg}' ({input_type}) for {next_model_type} from {prev_model_type}")

        return injected_args

# Global instance
translator = DataTranslator()
