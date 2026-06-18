"""
Django views for RISC-V Assembly Compiler.

This module provides endpoints for compiling C code to RISC-V assembly
and serving the compiler visualizer interface.
"""
import os
import json
import re
import subprocess
import tempfile

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


def index(request):
    """Render the main compiler visualizer page."""
    return render(request, 'compiler/index.html')


def clean_assembly(asm_text):
    """
    Clean and filter GCC-generated assembly text.
    
    Removes unnecessary directives, debug metadata, and source code comments
    to produce readable RISC-V assembly suitable for educational purposes.
    
    Args:
        asm_text (str): Raw assembly output from GCC.
        
    Returns:
        str: Cleaned assembly text.
    """
    cleaned_lines = []
    debug_section_started = False
    
    for line in asm_text.splitlines():
        stripped = line.strip()
        
        # Stop reading at debug sections
        if stripped.startswith('.section') and '.debug' in stripped:
            debug_section_started = True
            break
        
        # Skip compiler header comments
        if any(stripped.startswith(prefix) for prefix in 
               ('# GNU C', '# GGC', '# options passed')):
            continue
        
        # Clean source code injection: "# /tmp/tmptvzcpanx.c:2: int a = 5;" → "# int a = 5;"
        if stripped.startswith('#') and '.c:' in stripped:
            parts = stripped.split(':', 2)
            if len(parts) == 3:
                cleaned_lines.append(f"\n    # {parts[2].strip()}")
            continue
        
        # Remove assembler directives that clutter output
        if stripped.startswith('.'):
            ignore_prefixes = (
                '.file', '.option', '.attribute', '.cfi_',
                '.loc', '.globl', '.type', '.size', '.ident', '.align'
            )
            if stripped.startswith(ignore_prefixes):
                continue
            
            # Remove internal metadata labels
            if stripped.endswith(':') and any(
                marker in stripped for marker in ('Ltext', 'LFB', 'LFE', 'Letext')
            ):
                continue
        
        # Clean up GCC internal comments on instruction lines
        if '#' in line and not line.strip().startswith('#'):
            line = re.sub(r'#\s*tmp\d+.*', '', line)      # Remove "# tmp123, a"
            line = re.sub(r'#\s*_[a-zA-Z0-9]+.*', '', line)  # Remove "# _3, b"
            line = re.sub(r'#\s*,.*', '', line)           # Remove "#," or "##,"
            line = re.sub(r'#\s*$', '', line)             # Remove trailing "#"
            line = line.rstrip()
        
        # Skip empty lines created by cleaning
        if not stripped:
            continue
        
        cleaned_lines.append(line)
    
    return '\n'.join(cleaned_lines)


@csrf_exempt
def compile_code(request):
    """
    Compile C code to RISC-V assembly.
    
    Expects a POST request with JSON body containing 'code' field.
    Returns cleaned RISC-V assembly or an error message.
    
    Query Parameters:
        None
        
    Request Body:
        {
            "code": "C source code string"
        }
        
    Returns:
        JsonResponse: Either {'assembly': str} on success or {'error': str} on failure.
    """
    if request.method != 'POST':
        return JsonResponse(
            {'error': 'Invalid request method. Use POST.'},
            status=400
        )
    
    temp_files = {}
    
    try:
        data = json.loads(request.body)
        c_code = data.get('code', '').strip()
        
        if not c_code:
            return JsonResponse(
                {'error': 'No code provided.'},
                status=400
            )
        
        # Create temporary input file
        with tempfile.NamedTemporaryFile(
            suffix='.c',
            delete=False,
            mode='w',
            encoding='utf-8'
        ) as f_in:
            f_in.write(c_code)
            input_path = f_in.name
        
        temp_files['input'] = input_path
        output_path = input_path.replace('.c', '.s')
        temp_files['output'] = output_path
        
        # Compile C to assembly
        cmd = [
            'riscv64-linux-gnu-gcc',
            '-S',
            '-O0',                              # No optimization (educational purposes)
            '-fverbose-asm',                    # Add verbose assembly comments
            '-g',                               # Include debug symbols
            '-fno-asynchronous-unwind-tables',  # Reduce .cfi_ clutter
            input_path,
            '-o',
            output_path
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            encoding='utf-8'
        )
        
        if result.returncode != 0:
            return JsonResponse(
                {'error': result.stderr},
                status=400
            )
        
        # Read and clean the assembly
        with open(output_path, 'r', encoding='utf-8') as f_out:
            raw_assembly = f_out.read()
        
        clean_asm = clean_assembly(raw_assembly)
        
        return JsonResponse({'assembly': clean_asm})
    
    except json.JSONDecodeError:
        return JsonResponse(
            {'error': 'Invalid JSON in request body.'},
            status=400
        )
    except subprocess.TimeoutExpired:
        return JsonResponse(
            {'error': 'Compilation timed out. Code may be too complex.'},
            status=500
        )
    except Exception as e:
        return JsonResponse(
            {'error': f'Compilation error: {str(e)}'},
            status=500
        )
    
    finally:
        # Clean up temporary files
        for key, path in temp_files.items():
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass