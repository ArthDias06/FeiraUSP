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
    reg_state = {}  # Mapeia registrador -> valor numérico ou nome da variável
    mem_state = {}  # Mapeia offset (ex: '-20') -> {'name': 'var_name', 'value': 'valor'}
    current_c_line = ""
    last_var_declared = None
    
    for line in asm_text.splitlines():
        original_line = line
        line = line.strip()
        
        # 1. Rastrear código C inserido pelo clean_assembly
        if line.startswith('#'):
            current_c_line = line[1:].strip()
            
            # Tenta capturar tanto declarações quanto atribuições do C
            match_var = re.search(r'\b(?:int|char|short|long|float|double)\s+([a-zA-Z_]\w*)\s*[=;]', current_c_line)
            if not match_var:
                match_var = re.search(r'([a-zA-Z_]\w*)\s*=', current_c_line)
                
            if match_var:
                last_var_declared = match_var.group(1)
            
            explanations.append({
                "instruction": original_line,
                "explanation": f"Código C: '{current_c_line}'\nAs instruções abaixo traduzem este passo para a linguagem da máquina."
            })
            continue

        # Remove comentários inline para o parsing da instrução
        line = re.sub(r'#.*', '', line).strip()
        
        # Ignora linhas em branco, diretivas ou labels
        if not line or line.startswith('.') or line.endswith(':'):
            continue
            
        explanation = "Instrução de controle interno ou não mapeada no modelo didático básico."
        
        # 2. Tratamento de Pilha (Stack) e Frame Pointer
        match_stack = re.match(r'addi\s+sp,\s*sp,\s*(-?\d+)', line)
        if match_stack:
            val = int(match_stack.group(1))
            if val < 0:
                explanation = f"Reserva {abs(val)} bytes na Pilha (Stack). Este espaço guardará as variáveis locais de forma isolada na memória."
            else:
                explanation = f"Libera {val} bytes da Pilha (Stack). A função terminou, então o espaço de suas variáveis locais é devolvido."
                mem_state.clear()
                reg_state.clear()
                
        elif re.match(r's[dw]\s+s0,\s*\d+\(sp\)', line):
            explanation = "Salva o Frame Pointer (s0) anterior na pilha. Isso garante que o programa saiba voltar ao contexto de quem o chamou."
            
        elif re.match(r'addi\s+s0,\s*sp,\s*\d+', line):
            explanation = "Atualiza o Frame Pointer (s0). Ele servirá como 'ponto de referência' base para localizar as variáveis desta função."

        elif re.match(r'l[dw]\s+s0,\s*\d+\(sp\)', line):
            explanation = "Restaura o Frame Pointer (s0) original, devolvendo o controle da memória para a função de origem."

        # 3. Carregamento de Valores Imediatos
        elif match := re.match(r'li\s+([a-z0-9]+),\s*(-?\d+)', line):
            reg, val = match.groups()
            reg_state[reg] = val
            explanation = f"Carrega o número {val} diretamente no registrador '{reg}' para uso imediato."

        # 4. Operações de Memória (Store - Escrever na memória)
        elif match := re.match(r's([dwb])\s+([a-z0-9]+),\s*(-?\d+)\(([a-z0-9]+)\)', line):
            tipo, reg, offset, base = match.groups()
            val_to_store = reg_state.get(reg, "?")
            
            # Se a base for o Frame Pointer (s0), trata-se de variável local
            if base == 's0':
                var_name = last_var_declared if last_var_declared else mem_state.get(offset, {}).get('name', f"Variável no offset {offset}")
                mem_state[offset] = {'name': var_name, 'value': val_to_store}
                
                # Consome a variável declarada para não poluir os próximos stores
                last_var_declared = None 
                
                abs_offset = abs(int(offset))
                explanation = f"Guarda o valor armazenado em '{reg}' (atualmente contendo {val_to_store}) na variável local '{var_name}'. (endereço offset {abs_offset})"
            else:
                explanation = f"Salva o valor de '{reg}' diretamente no endereço apontado por {offset}({base})."

        # 5. Operações de Memória (Load - Ler da memória)
        elif match := re.match(r'l([dwb])[a-z]*\s+([a-z0-9]+),\s*(-?\d+)\(([a-z0-9]+)\)', line):
            tipo, reg, offset, base = match.groups()
            
            if base == 's0':
                mem_info = mem_state.get(offset, {'name': f"memória no offset {offset}", 'value': '?'})
                reg_state[reg] = mem_info['name']
                abs_offset = abs(int(offset))
                explanation = f"Lê o valor da variável local '{mem_info['name']}' (endereço offset {abs_offset}) e o traz para o registrador '{reg}', para que o processador possa usá-lo."
            else:
                explanation = f"Carrega dados da memória (endereço {offset} a partir de {base}) para o registrador '{reg}'."

        # 6. Operações Aritméticas e Lógicas
        elif match := re.match(r'(add|sub|mul|div)w?\s+([a-z0-9]+),\s*([a-z0-9]+),\s*([a-z0-9]+)', line):
            op, dest, src1, src2 = match.groups()
            
            val1 = reg_state.get(src1, f"'{src1}'")
            val2 = reg_state.get(src2, f"'{src2}'")
            
            op_map = {"add": "+", "sub": "-", "mul": "*", "div": "/"}
            expressao = f"({val1} {op_map[op]} {val2})"
            reg_state[dest] = expressao
            
            acao = "Soma" if op == "add" else "Subtrai" if op == "sub" else "Multiplica" if op == "mul" else "Divide"
            explanation = f"{acao} {val1} e {val2}. O resultado matemático {expressao} fica guardado no registrador '{dest}'."

        elif match := re.match(r'addiw?\s+([a-z0-9]+),\s*([a-z0-9]+),\s*(-?\d+)', line):
            dest, src, imm = match.groups()
            if dest != 'sp' and src != 'sp': # Garante que não sobrescreve os avisos de Pilha
                val = reg_state.get(src, f"'{src}'")
                expressao = f"({val} + {imm})"
                reg_state[dest] = expressao
                explanation = f"Adiciona a constante {imm} ao conteúdo de {val}. O resultado vai para '{dest}' (muito comum em contadores como i++)."

        # 7. Movimentação de Registradores
        elif match := re.match(r'mv\s+([a-z0-9]+),\s*([a-z0-9]+)', line):
            dest, src = match.groups()
            conteudo = reg_state.get(src, "valor desconhecido")
            reg_state[dest] = conteudo
            explanation = f"Copia o dado de '{src}' (que contém {conteudo}) para '{dest}'. É uma transferência rápida entre os rascunhos da CPU."

        # 8. Saltos e Condicionais (If, Else, Loops)
        elif match := re.match(r'(beq|bne|blt|bge|ble|bgt)\s+([a-z0-9]+),\s*([a-z0-9]+),\s*([a-zA-Z0-9_.]+)', line):
            op, reg1, reg2, label = match.groups()
            cond_map = {"beq": "==", "bne": "!=", "blt": "<", "bge": ">=", "ble": "<=", "bgt": ">"}
            cond = cond_map.get(op, "??")
            
            v1 = reg_state.get(reg1, reg1)
            v2 = reg_state.get(reg2, reg2)
            explanation = f"Desvio condicional (If/Loop): Verifica se {v1} {cond} {v2}. Se a condição for verdadeira, pula imediatamente para o bloco '{label}'."
            
        elif match := re.match(r'j\s+([a-zA-Z0-9_.]+)', line):
            label = match.group(1)
            explanation = f"Salto direto: O programa obrigatoriamente pula o fluxo de execução para a instrução marcada como '{label}'."

        # 9. Funções
        elif line.startswith('call '):
            func = line.split()[1]
            explanation = f"Pausa a execução atual e chama a função '{func}'. O processador anota de onde parou para conseguir retornar depois."

        elif line == 'ret':
            explanation = "Finaliza a função atual. O processador lê a anotação que fez e retorna ao ponto exato do programa que o havia chamado."

        elif line == 'nop':
            explanation = "Nenhuma operação (No-Op). Usado para gerar um compasso de espera ou alinhar blocos na memória de forma otimizada."

        # Adiciona a instrução atual e sua explicação formatada
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