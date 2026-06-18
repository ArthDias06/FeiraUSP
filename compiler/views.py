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

def _format_valor(valor):
    """
    Formata um valor (número, nome de variável ou expressão) para exibição
    amigável dentro de uma explicação em português.
    """
    if valor is None:
        return "um valor ainda não rastreado"
    return str(valor)


def _nome_funcao_amigavel(func):
    """
    Retorna uma descrição mais natural para chamadas a funções conhecidas
    da biblioteca padrão, ou None caso a função não seja conhecida.
    """
    conhecidas = {
        'printf': "exibir algo na tela (printf)",
        'scanf': "ler algo digitado pelo usuário (scanf)",
        'puts': "exibir uma linha de texto na tela (puts)",
        'putchar': "exibir um único caractere na tela (putchar)",
        'malloc': "reservar memória dinamicamente (malloc)",
        'free': "liberar memória reservada dinamicamente (free)",
        'strlen': "calcular o tamanho de um texto (strlen)",
        'strcmp': "comparar dois textos (strcmp)",
        'strcpy': "copiar um texto para outro lugar (strcpy)",
    }
    return conhecidas.get(func)


def generate_explanations(asm_text):
    """
    Analisa o código assembly limpo e gera explicações didáticas para as instruções,
    rastreando o estado dos registradores e variáveis mapeadas na memória (stack).

    Cada explicação inclui, sempre que possível, o trecho de código C de origem
    como contexto, além de descrever o efeito da instrução em linguagem natural,
    citando os valores conhecidos dos registradores/variáveis envolvidos.
    """
    explanations = []

    # --- Contexto e Estado ---
    reg_state = {}        # Mapeia registrador -> valor numérico, nome de variável ou expressão
    mem_state = {}        # Mapeia offset (ex: '-20') -> {'name': 'var_name', 'value': 'valor'}
    current_c_line = ""   # Última linha de código C vista (usada como contexto nas explicações)
    last_var_declared = None  # Nome da variável que a próxima instrução 'store' deve preencher
    pending_call_args = []    # Descrições dos argumentos preparados antes da próxima 'call'

    # Nomes amigáveis para os registradores de argumento/retorno mais comuns,
    # para deixar as explicações menos "técnicas demais" para quem está aprendendo.
    arg_regs = {
        'a0': 'a0 (valor de retorno)',
        'a1': 'a1',
        'a2': 'a2',
        'a3': 'a3',
        'a4': 'a4',
        'a5': 'a5',
        'a6': 'a6',
        'a7': 'a7',
    }

    def reg_label(reg):
        """Retorna um rótulo amigável para o registrador, se houver um conhecido."""
        return arg_regs.get(reg, f"'{reg}'")

    for line in asm_text.splitlines():
        original_line = line
        line = line.strip()

        # 1. Rastrear código C inserido pelo clean_assembly.
        #    Essas linhas servem de "título" para o bloco de instruções que vem a seguir,
        #    e o texto delas é reaproveitado como contexto em cada explicação subsequente.
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
                "explanation": (
                    f"Código C correspondente: \"{current_c_line}\"\n"
                    f"As próximas instruções em Assembly mostram, passo a passo, como o processador "
                    f"executa essa linha do programa original."
                )
            })
            continue

        # Remove comentários inline para o parsing da instrução
        line = re.sub(r'#.*', '', line).strip()

        # Ignora linhas em branco, diretivas ou labels
        if not line or line.startswith('.') or line.endswith(':'):
            continue

        explanation = (
            "Instrução de controle interno do compilador, não detalhada neste modelo didático "
            "(geralmente envolve ajustes internos que não alteram a lógica do seu código C)."
        )

        # 2. Tratamento de Pilha (Stack) e Frame Pointer
        match_stack = re.match(r'addi\s+sp,\s*sp,\s*(-?\d+)', line)
        if match_stack:
            val = int(match_stack.group(1))
            if val < 0:
                explanation = (
                    f"Reserva {abs(val)} bytes na Pilha (memória usada para variáveis locais). "
                    f"É como separar uma 'gaveta' só para esta função guardar suas variáveis "
                    f"sem interferir em outras partes do programa."
                )
            else:
                explanation = (
                    f"Libera os {val} bytes que haviam sido reservados na Pilha. "
                    f"A função está terminando, então o espaço usado pelas suas variáveis "
                    f"locais é devolvido para o sistema."
                )
                mem_state.clear()
                reg_state.clear()

        elif re.match(r's[dw]\s+s0,\s*\d+\(sp\)', line):
            explanation = (
                "Guarda na Pilha o valor anterior do Frame Pointer (registrador s0). "
                "Isso preserva a referência da função que chamou esta, para que ela possa "
                "ser restaurada corretamente quando esta função terminar."
            )

        elif re.match(r'addi\s+s0,\s*sp,\s*\d+', line):
            explanation = (
                "Define um novo Frame Pointer (s0) para esta função. A partir daqui, "
                "s0 funciona como um 'ponto de referência fixo' usado para localizar "
                "cada variável local desta função na memória."
            )

        elif re.match(r'l[dw]\s+s0,\s*\d+\(sp\)', line):
            explanation = (
                "Restaura o Frame Pointer (s0) que pertencia à função anterior, devolvendo "
                "a ela a referência correta para suas próprias variáveis."
            )

        # 3. Carregamento de Valores Imediatos
        elif match := re.match(r'li\s+([a-z0-9]+),\s*(-?\d+)', line):
            reg, val = match.groups()
            reg_state[reg] = val
            explanation = (
                f"Coloca o número {val} diretamente no registrador {reg_label(reg)}, "
                f"deixando esse valor pronto para ser usado na próxima operação."
            )

        # 4. Operações de Memória (Store - Escrever na memória)
        elif match := re.match(r's([dwb])\s+([a-z0-9]+),\s*(-?\d+)\(([a-z0-9]+)\)', line):
            tipo, reg, offset, base = match.groups()
            val_to_store = _format_valor(reg_state.get(reg))

            # Se a base for o Frame Pointer (s0), trata-se de variável local
            if base == 's0':
                var_name = last_var_declared if last_var_declared else mem_state.get(offset, {}).get('name', f"variável do offset {offset}")
                mem_state[offset] = {'name': var_name, 'value': reg_state.get(reg)}

                # Consome a variável declarada para não poluir os próximos stores
                last_var_declared = None

                abs_offset = abs(int(offset))
                explanation = (
                    f"Copia o valor que está em {reg_label(reg)} ({val_to_store}) para a variável "
                    f"local '{var_name}', guardada na memória da pilha (offset {abs_offset}). "
                    f"Em outras palavras: '{var_name}' agora vale {val_to_store}."
                )
            else:
                explanation = (
                    f"Escreve o valor de {reg_label(reg)} diretamente no endereço de memória "
                    f"calculado como {offset}({base}) — geralmente usado para vetores, structs "
                    f"ou ponteiros, e não para uma variável simples."
                )

        # 5. Operações de Memória (Load - Ler da memória)
        elif match := re.match(r'l([dwb])[a-z]*\s+([a-z0-9]+),\s*(-?\d+)\(([a-z0-9]+)\)', line):
            tipo, reg, offset, base = match.groups()

            if base == 's0':
                mem_info = mem_state.get(offset, {'name': f"variável do offset {offset}", 'value': None})
                reg_state[reg] = mem_info['name']
                abs_offset = abs(int(offset))
                explanation = (
                    f"Busca o valor da variável local '{mem_info['name']}' (offset {abs_offset} na pilha) "
                    f"e o traz para o registrador {reg_label(reg)}, deixando-o disponível para a "
                    f"próxima operação que o processador for executar."
                )
            else:
                explanation = (
                    f"Lê um valor da memória, no endereço calculado como {offset}({base}), e o "
                    f"coloca no registrador {reg_label(reg)} — padrão comum ao acessar vetores, "
                    f"structs ou dados apontados por um ponteiro."
                )

        # 6. Operações Aritméticas e Lógicas
        elif match := re.match(r'(add|sub|mul|div|rem|and|or|xor)w?\s+([a-z0-9]+),\s*([a-z0-9]+),\s*([a-z0-9]+)', line):
            op, dest, src1, src2 = match.groups()

            val1 = _format_valor(reg_state.get(src1, f"o conteúdo de '{src1}'"))
            val2 = _format_valor(reg_state.get(src2, f"o conteúdo de '{src2}'"))

            op_map = {
                "add": "+", "sub": "-", "mul": "*", "div": "/", "rem": "%",
                "and": "&", "or": "|", "xor": "^",
            }
            acao_map = {
                "add": "Soma", "sub": "Subtrai", "mul": "Multiplica", "div": "Divide",
                "rem": "Calcula o resto da divisão entre", "and": "Aplica um 'E' lógico (AND) entre",
                "or": "Aplica um 'OU' lógico (OR) entre", "xor": "Aplica um 'OU exclusivo' (XOR) entre",
            }
            simbolo = op_map.get(op, op)
            expressao = f"({val1} {simbolo} {val2})"
            reg_state[dest] = expressao

            acao = acao_map.get(op, "Combina")
            explanation = (
                f"{acao} {val1} e {val2}. O resultado, {expressao}, é guardado no registrador "
                f"{reg_label(dest)} para ser usado a seguir."
            )

        elif match := re.match(r'addiw?\s+([a-z0-9]+),\s*([a-z0-9]+),\s*(-?\d+)', line):
            dest, src, imm = match.groups()
            if dest != 'sp' and src != 'sp':  # Garante que não sobrescreve os avisos de Pilha
                val = _format_valor(reg_state.get(src, f"o conteúdo de '{src}'"))
                sinal = "+" if int(imm) >= 0 else "-"
                imm_abs = abs(int(imm))
                expressao = f"({val} {sinal} {imm_abs})"
                reg_state[dest] = expressao
                explanation = (
                    f"Soma o número {imm} ao valor {val}. O resultado, {expressao}, vai para o "
                    f"registrador {reg_label(dest)}. Esse tipo de instrução é muito comum em "
                    f"contadores de laço (como em 'i++' ou 'i = i + 1')."
                )

        # 7. Movimentação de Registradores
        elif match := re.match(r'mv\s+([a-z0-9]+),\s*([a-z0-9]+)', line):
            dest, src = match.groups()
            conteudo = _format_valor(reg_state.get(src))
            reg_state[dest] = reg_state.get(src)

            # Se o destino é um registrador de argumento, é provável que esteja
            # preparando uma chamada de função (ex.: mv a0, s0 antes de um 'call').
            if dest in arg_regs:
                pending_call_args.append(f"{reg_label(dest)} recebe {conteudo}")
                explanation = (
                    f"Copia o valor {conteudo} (vindo de '{src}') para {reg_label(dest)}, "
                    f"preparando esse dado para ser usado como argumento na próxima chamada de função."
                )
            else:
                explanation = (
                    f"Copia o valor de '{src}' (que contém {conteudo}) para '{dest}'. É uma simples "
                    f"transferência entre dois 'rascunhos' (registradores) do processador."
                )

        # 8. Saltos e Condicionais (If, Else, Loops)
        elif match := re.match(r'(beq|bne|blt|bge|ble|bgt|bltu|bgeu)\s+([a-z0-9]+),\s*([a-z0-9]+),\s*([a-zA-Z0-9_.]+)', line):
            op, reg1, reg2, label = match.groups()
            cond_map = {
                "beq": "é igual a", "bne": "é diferente de", "blt": "é menor que",
                "bge": "é maior ou igual a", "ble": "é menor ou igual a", "bgt": "é maior que",
                "bltu": "é menor que (sem sinal)", "bgeu": "é maior ou igual a (sem sinal)",
            }
            cond = cond_map.get(op, "satisfaz uma condição em relação a")

            v1 = _format_valor(reg_state.get(reg1, reg1))
            v2 = _format_valor(reg_state.get(reg2, reg2))

            # Saltos "para trás" no código costumam indicar voltas de laço (loop);
            # já saltos "para a frente" costumam indicar a saída de um if/else.
            destino_provavel = "voltar ao início de um laço (loop)" if label.split('.')[-1].isdigit() else "pular um bloco de código (if/else)"

            explanation = (
                f"Testa se {v1} {cond} {v2}. Se essa condição for verdadeira, o programa desvia "
                f"sua execução diretamente para o ponto '{label}' — normalmente usado para "
                f"{destino_provavel}. Se for falsa, a execução simplesmente continua na linha seguinte."
            )

        elif match := re.match(r'j\s+([a-zA-Z0-9_.]+)', line):
            label = match.group(1)
            explanation = (
                f"Salto incondicional: o programa pula direto para o ponto marcado como '{label}', "
                f"sem nenhuma verificação. É comum aparecer ao final de um bloco 'if' (para pular o "
                f"'else') ou ao final do corpo de um laço (para voltar e checar a condição de novo)."
            )

        # 9. Funções
        elif line.startswith('call ') or re.match(r'call\s+', line):
            func = line.split()[1].split('@')[0]
            descricao_amigavel = _nome_funcao_amigavel(func)

            if pending_call_args:
                args_texto = "; ".join(pending_call_args)
                contexto_args = f" Antes desta chamada, os seguintes argumentos foram preparados: {args_texto}."
            else:
                contexto_args = ""

            if descricao_amigavel:
                explanation = (
                    f"Chama a função '{func}', responsável por {descricao_amigavel}.{contexto_args} "
                    f"O processador guarda o ponto exato de onde saiu para conseguir voltar depois "
                    f"que '{func}' terminar."
                )
            else:
                explanation = (
                    f"Chama a função '{func}', definida em outra parte do seu código C.{contexto_args} "
                    f"O processador guarda o ponto exato de onde saiu para conseguir voltar depois "
                    f"que '{func}' terminar."
                )

            pending_call_args = []  # Reinicia o rastreamento de argumentos para a próxima chamada

        elif line == 'ret':
            explanation = (
                "Encerra a função atual: o processador consulta o endereço de retorno que havia "
                "guardado e volta exatamente para o ponto do programa de onde essa função foi chamada "
                "(geralmente levando o resultado, se houver, no registrador a0)."
            )

        elif line == 'nop':
            explanation = (
                "Não faz nada (No-Operation). É usada apenas para preencher espaço ou alinhar "
                "blocos de instruções na memória, sem efeito sobre a lógica do programa."
            )

        elif match := re.match(r'sext\.w\s+([a-z0-9]+),\s*([a-z0-9]+)', line):
            dest, src = match.groups()
            conteudo = _format_valor(reg_state.get(src))
            reg_state[dest] = reg_state.get(src)
            explanation = (
                f"Ajusta o valor de '{src}' ({conteudo}) para que ocupe corretamente os 64 bits do "
                f"registrador {reg_label(dest)}, preservando seu sinal (positivo/negativo). É um "
                f"detalhe técnico necessário quando se trabalha com números de 32 bits (como 'int') "
                f"em um processador de 64 bits."
            )

        elif match := re.match(r's(?:ll|rl|ra)i?w?\s+([a-z0-9]+),\s*([a-z0-9]+),\s*(-?\d+)', line):
            dest, src, qtd = match.groups()
            conteudo = _format_valor(reg_state.get(src, f"o conteúdo de '{src}'"))
            direcao = "esquerda" if line.startswith('sll') else "direita"
            expressao = f"({conteudo} deslocado {qtd} posições para a {direcao})"
            reg_state[dest] = expressao
            explanation = (
                f"Desloca os bits de {conteudo} em {qtd} posições para a {direcao}. O resultado, "
                f"{expressao}, vai para {reg_label(dest)}. Esse tipo de operação costuma aparecer em "
                f"multiplicações/divisões por potências de 2 ou em manipulação de bits."
            )

        # Monta um texto de contexto com a linha C atual (quando disponível), seguido
        # da explicação específica da instrução, para reforçar a relação entre o
        # código original e o assembly gerado.
        if current_c_line:
            texto_final = f"(Referente a: \"{current_c_line}\")\n{explanation}"
        else:
            texto_final = explanation

        # Adiciona a instrução atual e sua explicação formatada
        explanations.append({
            "instruction": original_line.strip(),
            "explanation": texto_final
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