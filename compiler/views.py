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
        if re.match(r'^#\s*(GNU C|GGC|options passed|compiled by)', stripped):
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


def generate_explanations(asm_text):
    """
    Analisa o código assembly limpo e gera explicações didáticas para as instruções,
    rastreando o estado dos registradores e variáveis mapeadas na memória (stack).
    """
    explanations = []
    
    # --- Contexto e Estado ---
    reg_state = {}  # Mapeia registrador -> valor numérico ou nome da variável contida
    mem_state = {}  # Mapeia offset (ex: '-20') -> {'name': 'var_name', 'value': 'valor'}
    current_c_line = ""
    last_var_declared = None
    
    for line in asm_text.splitlines():
        original_line = line
        line = line.strip()
        
        # 1. Rastrear código C inserido pelo clean_assembly
        if line.startswith('#'):
            current_c_line = line[1:].strip()
            
            # Tenta extrair o nome da variável que está sendo declarada ou atribuída no C
            # Cobre casos como "int a = 5;" ou "soma = a + b;"
            match_var = re.search(r'\b(?:int|char|short|long|float|double)\s+([a-zA-Z_]\w*)\s*[=;]', current_c_line)
            if not match_var:
                match_var = re.search(r'([a-zA-Z_]\w*)\s*=', current_c_line)
                
            if match_var:
                last_var_declared = match_var.group(1)
            
            explanations.append({
                "instruction": original_line,
                "explanation": f"Contexto C: {current_c_line}"
            })
            continue

        # Remove comentários inline para o parsing da instrução
        line = re.sub(r'#.*', '', line).strip()
        
        # Ignora linhas em branco, diretivas ou labels
        if not line or line.startswith('.') or line.endswith(':'):
            continue
            
        explanation = "Instrução não mapeada pelo tradutor didático."
        
        # 2. Tratamento de Pilha (Stack) e Frame Pointer
        match_stack = re.match(r'addi\s+sp,sp,(-?\d+)', line.replace(' ', ''))
        if match_stack:
            val = int(match_stack.group(1))
            if val < 0:
                explanation = f"Reserva {abs(val)} bytes na pilha (Stack) para o escopo das variáveis locais."
            else:
                explanation = f"Libera {val} bytes da pilha, destruindo o escopo antes de finalizar a função."
                mem_state.clear() # Limpa o contexto de memória local ao sair do escopo
                
        elif re.match(r's[dw]\s+s0,\d+\(sp\)', line.replace(' ', '')):
            explanation = "Salva o endereço do Frame Pointer (s0) anterior na pilha."
            
        elif re.match(r'addi\s+s0,sp,\d+', line.replace(' ', '')):
            explanation = "Configura o novo Frame Pointer (s0) para a base do escopo da função."

        elif re.match(r'l[dw]\s+s0,\d+\(sp\)', line.replace(' ', '')):
            explanation = "Restaura o Frame Pointer (s0) original da função chamadora."

        # 3. Carregamento de Valores Imediatos
        elif match := re.match(r'li\s+([a-z0-9]+),\s*(-?\d+)', line):
            reg, val = match.groups()
            reg_state[reg] = val  # Atualiza estado do registrador
            explanation = f"Carrega o valor numérico {val} diretamente para o registrador '{reg}'."

        # 4. Operações de Memória (Store - Escrever na memória)
        elif match := re.match(r's([dwb])\s+([a-z0-9]+),\s*(-?\d+)\(([a-z0-9]+)\)', line):
            tipo, reg, offset, base = match.groups()
            size_name = "palavra" if tipo == 'w' else "palavra dupla" if tipo == 'd' else "byte"
            
            val_to_store = reg_state.get(reg, "?")
            
            # Associa a variável do C ao endereço de memória
            var_name = last_var_declared if last_var_declared else mem_state.get(offset, {}).get('name', f"mem[{offset}]")
            
            mem_state[offset] = {'name': var_name, 'value': val_to_store}
            
            explanation_target = f"variável '{var_name}' (offset {offset})" if var_name != f"mem[{offset}]" else f"posição {offset}({base})"
            explanation = f"Salva o valor {val_to_store} contido em '{reg}' na {explanation_target}."
            
            last_var_declared = None # Reseta após associar ao store

        # 5. Operações de Memória (Load - Ler da memória)
        elif match := re.match(r'l([dwb])[a-z]*\s+([a-z0-9]+),\s*(-?\d+)\(([a-z0-9]+)\)', line):
            tipo, reg, offset, base = match.groups()
            
            # Resgata do estado da memória o que estamos lendo
            mem_info = mem_state.get(offset, {'name': f"offset {offset}", 'value': '?'})
            reg_state[reg] = mem_info['name'] # O reg passa a representar o nome/valor da variável lida
            
            explanation = f"Lê o conteúdo da {mem_info['name']} e carrega no registrador '{reg}'."

        # 6. Operações Aritméticas
        elif match := re.match(r'(add|sub|mul)w?\s+([a-z0-9]+),\s*([a-z0-9]+),\s*([a-z0-9]+)', line):
            op, dest, src1, src2 = match.groups()
            
            val1 = reg_state.get(src1, src1)
            val2 = reg_state.get(src2, src2)
            
            op_symbol = "+" if op == "add" else "-" if op == "sub" else "*"
            expressao_resultante = f"({val1} {op_symbol} {val2})"
            
            reg_state[dest] = expressao_resultante # Ex: reg_state['a5'] vira "(a + b)"
            
            acao = "Soma" if op == "add" else "Subtrai" if op == "sub" else "Multiplica"
            termos = f"'{src2}' ({val2}) de '{src1}' ({val1})" if op == "sub" else f"'{src1}' ({val1}) e '{src2}' ({val2})"
            
            explanation = f"{acao} {termos}, guardando o resultado {expressao_resultante} em '{dest}'."

        # 7. Movimentação e Saltos
        elif match := re.match(r'mv\s+([a-z0-9]+),\s*([a-z0-9]+)', line):
            dest, src = match.groups()
            reg_state[dest] = reg_state.get(src, "?") # Propaga o estado
            explanation = f"Copia o valor/conteúdo de '{src}' ({reg_state[dest]}) para '{dest}'."

        elif line.startswith('call '):
            func = line.split()[1]
            explanation = f"Salva o endereço de retorno e pula para a execução da função '{func}'."

        elif line == 'ret':
            explanation = "Retorna o fluxo de execução para o endereço salvo pela função que fez a chamada."

        elif line == 'nop':
            explanation = "Nenhuma operação (No Operation). Geralmente inserido para alinhamento de pipeline."

        # Adiciona a explicação ao resultado
        explanations.append({
            "instruction": original_line.strip(),
            "explanation": explanation
        })
        
    return explanations

@csrf_exempt
def compile_code(request):
    """
    Compile C code to RISC-V assembly.
    
    Expects a POST request with JSON body containing 'code' field.
    Returns cleaned RISC-V assembly and explanations or an error message.
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
        explanations = generate_explanations(clean_asm)
        
        return JsonResponse({
            'assembly': clean_asm,
            'explanations': explanations
        })
    
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