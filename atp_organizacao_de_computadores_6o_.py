


Estrutura do arquivo:
    1.) Funções auxiliares (ctypes)  - conversão signed/unsigned de 32 bits
    2.) Memória, VRAM, Periféricos   - mapa de memória do projeto
    3.) Barramento                   - dados (32 bits), endereços (32 bits) e controle
    4.) CPU                          - ciclo único: busca, decodificação, execução, PC
    5.) Montador (auxiliar)          - gera o código de máquina do programa de teste
    6.) Programa de teste e testes   - exercita todas as funcionalidades

import ctypes

#0X00000 - 0x7FFFF = Memória RAM
#0x80000 - 0x8FFFF VRAM(gravados via instrução)
#0x90000 - 0x9FBFF Área reservada para expansão futura
#0x9FC00 - 0x9FFFF(Periféricos I/O)

RAM_INICIO, RAM_TAMANHO = 0x00000, 0x80000
VRAM_INICIO, VRAM_TAMANHO = 0x80000, 0x10000
IO_INICIO, IO_TAMANHO = 0x9FC00, 0x00400

COLUNAS = 32                      # A memória é uma matriz com 32 colunas (bytes por linha)

#Registradores mapeados em memória (E/S mapeada)
TERMINAL_TX = 0x9FC00             # escrita: envia 1 caractere ASCII para o terminal
TECLADO_DADO = 0x9FC04            # leitura: próxima tecla da fila (limpa a requisição)
TECLADO_STATUS = 0x9FC08          # leitura: bit 0 = existe tecla na fila

#Mapa de memória usado pelo simulador
VETOR_INTERRUPCAO = 0x00000100    # trecho da RAM com as instruções da interrupção
INICIO_PROGRAMA = 0x00000400      # endereço onde o programa principal é carregado

#CSRs mínimos para tratar interrupção (extensão Zicsr, nível máquina)
MSTATUS, MTVEC, MEPC, MCAUSE = 0x300, 0x305, 0x341, 0x342
MSTATUS_MIE, MSTATUS_MPIE = 1 << 3, 1 << 7
CAUSA_INT_EXTERNA = 0x8000000B

NOMES_ABI = ["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0", "s1",
             "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7",
             "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11",
             "t3", "t4", "t5", "t6"]


#------------------------------------------------------------------------------
# 1.) FUNÇÕES AUXILIARES
#------------------------------------------------------------------------------
#Casting entre inteiros usando ctypes:
#https://stackoverflow.com/questions/17166334/casting-between-integer-formats-with-python-ctypes
def u32(valor):
    #Qualquer inteiro vira um valor de 32 bits SEM sinal (descarta o excedente)
    return ctypes.c_uint32(valor).value

def s32(valor):
    #Qualquer inteiro vira um valor de 32 bits COM sinal (complemento de 2)
    return ctypes.c_int32(valor).value

def sext(valor, bits):
    #Extensão de sinal: se o bit mais significativo (à esquerda) é 1, completa com 1s
    valor &= (1 << bits) - 1
    return ctypes.c_int32(valor << (32 - bits)).value >> (32 - bits)


class BusError(Exception):
    """Endereço sem dispositivo ou sem permissão no barramento de controle"""

class InstrucaoIlegal(Exception):
    """Instrução que não pertence ao RV32I"""


#------------------------------------------------------------------------------
# 2.) MEMÓRIA, VRAM E PERIFÉRICOS
#------------------------------------------------------------------------------
class Memoria:
    """Matriz de COLUNAS bytes por linha. Cada faixa do mapa (setor) é uma instância."""
    def __init__(self, nome, base, tamanho, permissao="RW"):
        self.nome, self.base, self.tamanho, self.permissao = nome, base, tamanho, permissao
        self.linhas = tamanho // COLUNAS
        self.matriz = ((ctypes.c_uint8 * COLUNAS) * self.linhas)()

    def contem(self, addr, n=1):
        return self.base <= addr and addr + n <= self.base + self.tamanho

    def ler(self, addr, n):
        #Little-endian: o byte menos significativo fica no menor endereço
        valor = 0
        for i in range(n):
            off = addr - self.base + i
            valor |= self.matriz[off // COLUNAS][off % COLUNAS] << (8 * i)
        return valor

    def escrever(self, addr, valor, n):
        for i in range(n):
            off = addr - self.base + i
            self.matriz[off // COLUNAS][off % COLUNAS] = (valor >> (8 * i)) & 0xFF


class VRAM(Memoria):
    """Vídeo RAM: cada linha da matriz (32 colunas) é uma linha de texto do terminal"""
    def __init__(self):
        super().__init__("VRAM", VRAM_INICIO, VRAM_TAMANHO)

    def exibir(self, linhas=8, titulo="VRAM"):
        #E/S programada: transmite o conteúdo da VRAM como caracteres ASCII
        print(f"┌─ {titulo} " + "─" * max(0, COLUNAS - len(titulo) - 3) + "┐")
        for l in range(linhas):
            texto = "".join(chr(b) if 32 <= b < 127 else " " for b in self.matriz[l])
            print("│" + texto + "│")
        print("└" + "─" * COLUNAS + "┘")


class Perifericos(Memoria):
    """Terminal (TX) e teclado (fila de teclas + requisição de interrupção)"""
    def __init__(self):
        super().__init__("Periféricos", IO_INICIO, IO_TAMANHO)
        self.fila = []

    @property
    def irq(self):
        #IRQ - Interrupt Request: ativa enquanto houver tecla não lida
        return len(self.fila) > 0

    def pressionar(self, tecla):
        self.fila.append(ord(tecla) if isinstance(tecla, str) else tecla)

    def ler(self, addr, n):
        if addr == TECLADO_DADO:
            return self.fila.pop(0) if self.fila else 0
        if addr == TECLADO_STATUS:
            return 1 if self.fila else 0
        return super().ler(addr, n)

    def escrever(self, addr, valor, n):
        if addr == TERMINAL_TX:
            print(chr(valor & 0xFF), end="", flush=True)
        else:
            super().escrever(addr, valor, n)


class Reservada(Memoria):
    """0x90000 - 0x9FBFF: reservada para expansão futura (acesso gera BusError)"""
    def __init__(self):
        super().__init__("Reservada", 0x90000, 0x9FC00 - 0x90000, permissao="")


#------------------------------------------------------------------------------
# 3.) BARRAMENTO
#------------------------------------------------------------------------------
class Barramento:
    """
    Barramento de endereços (32 bits) ..... self.endereco
    Barramento de dados (32 bits) ......... self.dados
    Barramento de controle ................ self.controle (LEITURA/ESCRITA) + permissão
    """
    LEITURA, ESCRITA = "R", "W"

    def __init__(self):
        self.ram = Memoria("RAM", RAM_INICIO, RAM_TAMANHO)
        self.vram = VRAM()
        self.reservada = Reservada()
        self.io = Perifericos()
        self.dispositivos = [self.ram, self.vram, self.reservada, self.io]
        self.endereco, self.dados, self.controle = 0, 0, None

    @property
    def irq(self):
        return self.io.irq

    def _selecionar(self, addr, n, controle):
        #Decodificador de endereços: escolhe o bloco que responde ao endereço
        for d in self.dispositivos:
            if d.contem(addr, n):
                if controle not in d.permissao:
                    raise BusError(f"{d.nome} não permite '{controle}' em 0x{addr:08X}")
                return d
        raise BusError(f"endereço 0x{addr:08X} não pertence a nenhum bloco")

    def ler(self, addr, n):
        addr = u32(addr)
        self.endereco, self.controle = addr, self.LEITURA
        self.dados = self._selecionar(addr, n, self.LEITURA).ler(addr, n)
        return self.dados

    def escrever(self, addr, valor, n):
        addr = u32(addr)
        self.endereco, self.controle = addr, self.ESCRITA
        self.dados = valor & ((1 << (8 * n)) - 1)
        self._selecionar(addr, n, self.ESCRITA).escrever(addr, self.dados, n)

    def read8(self, addr): return self.ler(addr, 1)
    def read16(self, addr): return self.ler(addr, 2)
    def read32(self, addr): return self.ler(addr, 4)
    def write8(self, addr, valor): self.escrever(addr, valor, 1)
    def write16(self, addr, valor): self.escrever(addr, valor, 2)
    def write32(self, addr, valor): self.escrever(addr, valor, 4)

    def carregar(self, addr, palavras):
        #Carrega um programa (lista de palavras de 32 bits) na memória
        for i, palavra in enumerate(palavras):
            self.write32(addr + 4 * i, palavra)


#------------------------------------------------------------------------------
# 4.) CPU
#------------------------------------------------------------------------------
class CPU:
    def __init__(self, bus):
        self.bus = bus
        self.pc = ctypes.c_uint32(INICIO_PROGRAMA)
        self.regs = [0] * 32                  # x0..x31 (valores de 32 bits sem sinal)
        self.regs[2] = RAM_INICIO + RAM_TAMANHO   # sp aponta para o topo da RAM
        self.csr = {MSTATUS: 0, MTVEC: VETOR_INTERRUPCAO, MEPC: 0, MCAUSE: 0}
        self.ciclos = 0
        self.parada = False
        self.codigo_saida = 0

    def escreve(self, rd, valor):
        #RegWrite: o registrador x0 é sempre zero e não aceita escrita
        if rd != 0:
            self.regs[rd] = u32(valor)

    def imm_gen(self, inst):
        #Imm Gen (fig. 5): monta o imediato conforme o formato e estende o sinal
        opcode = inst & 0x7F
        #TIPO I (aritméticas imediatas, loads, JALR)
        if opcode in (0x13, 0x03, 0x67):
            return sext(inst >> 20, 12)
        #TIPO S (stores): imm[11:5] = inst[31:25], imm[4:0] = inst[11:7]
        if opcode == 0x23:
            return sext(((inst >> 25) << 5) | ((inst >> 7) & 0x1F), 12)
        #TIPO B (branches): imm[12|10:5] = inst[31|30:25], imm[4:1|11] = inst[11:8|7]; bit 0 é sempre 0
        if opcode == 0x63:
            imm = (((inst >> 31) & 1) << 12) | (((inst >> 7) & 1) << 11) \
                | (((inst >> 25) & 0x3F) << 5) | (((inst >> 8) & 0xF) << 1)
            return sext(imm, 13)
        #TIPO U (LUI, AUIPC): imm[31:12] = inst[31:12], os 12 bits menores são 0
        if opcode in (0x37, 0x17):
            return s32(inst & 0xFFFFF000)
        #TIPO J (JAL): imm[20|10:1|11|19:12] = inst[31|30:21|20|19:12]; bit 0 é sempre 0
        if opcode == 0x6F:
            imm = (((inst >> 31) & 1) << 20) | (((inst >> 12) & 0xFF) << 12) \
                | (((inst >> 20) & 1) << 11) | (((inst >> 21) & 0x3FF) << 1)
            return sext(imm, 21)
        return 0

    def step(self):
        #1.) O contador de programa aponta para um endereço contido na memória principal
        pc = self.pc.value
        #2.) O conteúdo (binário/hexadecimal) é enviado para o processador
        inst = self.bus.read32(pc)

        #3.) O processador identifica qual instrução deve ser executada
        opcode = inst & 0x7F
        #Campos fixos (os formatos R/I/S/B/U/J compartilham as mesmas posições)
        rd, funct3, rs1, rs2, funct7 = (inst >> 7) & 0x1F, (inst >> 12) & 0x07, (inst >> 15) & 0x1F, (inst >> 20) & 0x1F, (inst >> 25) & 0x7F
        imm = self.imm_gen(inst)
        v1, v2 = self.regs[rs1], self.regs[rs2]      #Read data 1 e Read data 2
        prox_pc = u32(pc + 4)                        #PC + 4 (PCSrc = 0)

        #4.) Executa a instrução
        match opcode:
            #Tipo-R
            case 0x33:
                #Só existem funct7 = 0x00, ou 0x20 para SUB e SRA
                if funct7 not in (0x00, 0x20) or (funct7 == 0x20 and funct3 not in (0x0, 0x5)):
                    raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")
                #(ADD e SUB)
                if funct3 == 0x0:
                    self.escreve(rd, (v1 + v2) if funct7 == 0x00 else (v1 - v2))
                #SLL - SHIFT LEFT LOGICAL (só os 5 bits menores de rs2 contam)
                if funct3 == 0x1:
                    self.escreve(rd, v1 << (v2 & 0x1F))
                #SLT - SET LESS THAN (com sinal)
                if funct3 == 0x2:
                    self.escreve(rd, 1 if s32(v1) < s32(v2) else 0)
                #SLTU - SET LESS THAN unsigned, https://stackoverflow.com/questions/17166334/casting-between-integer-formats-with-python-ctypes
                if funct3 == 0x3:
                    self.escreve(rd, 1 if ctypes.c_uint32(v1).value < ctypes.c_uint32(v2).value else 0)
                #XOR
                if funct3 == 0x4:
                    self.escreve(rd, v1 ^ v2)
                #SRL e SRA (lógico preenche com 0, aritmético preserva o sinal)
                if funct3 == 0x5:
                    self.escreve(rd, (v1 >> (v2 & 0x1F)) if funct7 == 0x00 else (s32(v1) >> (v2 & 0x1F)))
                #OR
                if funct3 == 0x6:
                    self.escreve(rd, v1 | v2)
                #AND
                if funct3 == 0x7:
                    self.escreve(rd, v1 & v2)

            #Tipo-I (aritméticas/lógicas com imediato)
            case 0x13:
                #O imediato já vem com o sinal estendido para 32 bits (imm_gen)
                shamt = rs2                      #nos shifts o imediato é shamt = inst[24:20]
                #ADDI
                if funct3 == 0x0:
                    self.escreve(rd, v1 + imm)
                #SLTI (com sinal)
                if funct3 == 0x2:
                    self.escreve(rd, 1 if s32(v1) < imm else 0)
                #SLTIU (o imediato é estendido com sinal e depois comparado SEM sinal)
                if funct3 == 0x3:
                    self.escreve(rd, 1 if v1 < u32(imm) else 0)
                #XORI
                if funct3 == 0x4:
                    self.escreve(rd, v1 ^ u32(imm))
                #ORI
                if funct3 == 0x6:
                    self.escreve(rd, v1 | u32(imm))
                #ANDI
                if funct3 == 0x7:
                    self.escreve(rd, v1 & u32(imm))
                #SLLI Shift Left Logical Immediate
                if funct3 == 0x1:
                    if funct7 != 0x00:
                        raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")
                    self.escreve(rd, v1 << shamt)
                #SRLI e SRAI
                if funct3 == 0x5:
                    if funct7 not in (0x00, 0x20):
                        raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")
                    self.escreve(rd, (v1 >> shamt) if funct7 == 0x00 else (s32(v1) >> shamt))

            #Tipo-I (LOAD): rd = MEM[rs1 + imm]
            case 0x03:
                endereco = u32(v1 + imm)         #a ULA calcula o endereço
                #LB - load byte (estende o sinal)
                if funct3 == 0x0:
                    self.escreve(rd, sext(self.bus.read8(endereco), 8))
                #LH - load halfword (estende o sinal)
                elif funct3 == 0x1:
                    self.escreve(rd, sext(self.bus.read16(endereco), 16))
                #LW - load word
                elif funct3 == 0x2:
                    self.escreve(rd, self.bus.read32(endereco))
                #LBU - load byte unsigned
                elif funct3 == 0x4:
                    self.escreve(rd, self.bus.read8(endereco))
                #LHU - load halfword unsigned
                elif funct3 == 0x5:
                    self.escreve(rd, self.bus.read16(endereco))
                else:
                    raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")

            #Tipo-S (STORE): MEM[rs1 + imm] = rs2
            case 0x23:
                endereco = u32(v1 + imm)
                #SB
                if funct3 == 0x0:
                    self.bus.write8(endereco, v2)
                #SH
                elif funct3 == 0x1:
                    self.bus.write16(endereco, v2)
                #SW
                elif funct3 == 0x2:
                    self.bus.write32(endereco, v2)
                else:
                    raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")

            #Tipo-B (BRANCH): se a condição for verdadeira, PC = PC + imm
            case 0x63:
                #BEQ
                if funct3 == 0x0:
                    desvia = v1 == v2
                #BNE
                elif funct3 == 0x1:
                    desvia = v1 != v2
                #BLT (com sinal)
                elif funct3 == 0x4:
                    desvia = s32(v1) < s32(v2)
                #BGE (com sinal)
                elif funct3 == 0x5:
                    desvia = s32(v1) >= s32(v2)
                #BLTU (sem sinal)
                elif funct3 == 0x6:
                    desvia = v1 < v2
                #BGEU (sem sinal)
                elif funct3 == 0x7:
                    desvia = v1 >= v2
                else:
                    raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")
                if desvia:
                    prox_pc = u32(pc + imm)      #PCSrc = 1 (somador do desvio)

            #Tipo-J (JAL): rd = PC + 4; PC = PC + imm
            case 0x6F:
                self.escreve(rd, prox_pc)
                prox_pc = u32(pc + imm)

            #Tipo-I (JALR): rd = PC + 4; PC = (rs1 + imm) com o bit 0 zerado
            case 0x67:
                alvo = u32(v1 + imm) & 0xFFFFFFFE
                self.escreve(rd, prox_pc)        #v1 já foi lido (rd pode ser igual a rs1)
                prox_pc = alvo

            #Tipo-U (LUI): rd = imm[31:12] << 12
            case 0x37:
                self.escreve(rd, imm)

            #Tipo-U (AUIPC): rd = PC + (imm[31:12] << 12)
            case 0x17:
                self.escreve(rd, pc + imm)

            #FENCE e FENCE.I (simulador de um núcleo, sem cache de instruções: não faz nada)
            case 0x0F:
                if funct3 not in (0x0, 0x1):
                    raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")

            #SYSTEM (ECALL, EBREAK, MRET e instruções CSR)
            case 0x73:
                if funct3 == 0x0:
                    #ECALL - encerra o programa; o código de saída fica em a0
                    if inst == 0x00000073:
                        self.parada, self.codigo_saida = True, self.regs[10]
                    #EBREAK - ponto de parada
                    elif inst == 0x00100073:
                        self.parada, self.codigo_saida = True, self.regs[10]
                    #MRET - retorno da interrupção: PC = mepc e reabilita as interrupções
                    elif inst == 0x30200073:
                        mstatus = self.csr[MSTATUS]
                        mstatus = (mstatus | MSTATUS_MIE) if mstatus & MSTATUS_MPIE else (mstatus & ~MSTATUS_MIE)
                        self.csr[MSTATUS] = mstatus | MSTATUS_MPIE
                        prox_pc = self.csr[MEPC]
                    else:
                        raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")
                else:
                    #CSRRW/CSRRS/CSRRC (registrador) e CSRRWI/CSRRSI/CSRRCI (imediato de 5 bits em rs1)
                    csr = (inst >> 20) & 0xFFF
                    operacao = funct3 & 0x3
                    if operacao == 0 or funct3 == 0x4:
                        raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")
                    antigo = self.csr.get(csr, 0)
                    fonte = v1 if funct3 < 0x4 else rs1
                    if operacao == 0x1:                      #CSRRW(I): csr = fonte
                        novo = fonte
                    elif operacao == 0x2:                    #CSRRS(I): csr |= fonte (se rs1 != 0)
                        novo = (antigo | fonte) if rs1 != 0 else antigo
                    else:                                    #CSRRC(I): csr &= ~fonte (se rs1 != 0)
                        novo = (antigo & ~fonte) if rs1 != 0 else antigo
                    self.csr[csr] = u32(novo)
                    self.escreve(rd, antigo)

            case _:
                raise InstrucaoIlegal(f"PC=0x{pc:08X} inst=0x{inst:08X}")

        #5.) Atualiza o ProgramCount (e x0 continua sempre zero)
        self.pc.value = prox_pc
        self.regs[0] = 0
        self.ciclos += 1

        #6.) Ciclo de interrupção (fig. 7): só trata se as interrupções estiverem habilitadas
        if (self.csr[MSTATUS] & MSTATUS_MIE) and self.bus.irq:
            self.interrompe()

    def interrompe(self):
        #Salva o PC (retorno), desabilita novas interrupções e desvia para o tratador na RAM
        self.csr[MEPC] = self.pc.value
        self.csr[MCAUSE] = CAUSA_INT_EXTERNA
        mstatus = self.csr[MSTATUS]
        mstatus = (mstatus | MSTATUS_MPIE) if mstatus & MSTATUS_MIE else (mstatus & ~MSTATUS_MPIE)
        self.csr[MSTATUS] = mstatus & ~MSTATUS_MIE
        self.pc.value = self.csr[MTVEC]

    def run(self, max_instrucoes=100000, n_video=None, eventos=None):
        #n_video: a cada n instruções executadas a VRAM é transmitida para o terminal
        #eventos: {ciclo: função(cpu)} simula eventos externos (ex.: tecla pressionada)
        eventos = eventos or {}
        while not self.parada and self.ciclos < max_instrucoes:
            if self.ciclos in eventos:
                eventos[self.ciclos](self)
            self.step()
            if n_video and self.ciclos % n_video == 0:
                self.bus.vram.exibir(titulo=f"VRAM @ {self.ciclos} instr.")

    def dump(self):
        print(f"PC=0x{self.pc.value:08X}  instruções={self.ciclos}")
        for i in range(0, 32, 4):
            print("  ".join(f"{NOMES_ABI[j]:>4}=0x{self.regs[j]:08X}" for j in range(i, i + 4)))


#------------------------------------------------------------------------------
# 5.) MONTADOR (AUXILIAR) - transforma assembly RV32I em código de máquina
#------------------------------------------------------------------------------
#Não faz parte do hardware simulado: serve para gerar o programa de teste sem
#precisar de um compilador
REGISTRADORES = {nome: i for i, nome in enumerate(NOMES_ABI)}
REGISTRADORES.update({"fp": 8, **{f"x{i}": i for i in range(32)}})

TIPO_R = {"add": (0, 0x00), "sub": (0, 0x20), "sll": (1, 0x00), "slt": (2, 0x00), "sltu": (3, 0x00),
          "xor": (4, 0x00), "srl": (5, 0x00), "sra": (5, 0x20), "or": (6, 0x00), "and": (7, 0x00)}
TIPO_I = {"addi": 0, "slti": 2, "sltiu": 3, "xori": 4, "ori": 6, "andi": 7}
TIPO_I_SHIFT = {"slli": (1, 0x00), "srli": (5, 0x00), "srai": (5, 0x20)}
LOADS = {"lb": 0, "lh": 1, "lw": 2, "lbu": 4, "lhu": 5}
STORES = {"sb": 0, "sh": 1, "sw": 2}
BRANCHES = {"beq": 0, "bne": 1, "blt": 4, "bge": 5, "bltu": 6, "bgeu": 7}
CSR_NOMES = {"mstatus": MSTATUS, "mtvec": MTVEC, "mepc": MEPC, "mcause": MCAUSE}
CSR_OPS = {"csrrw": 1, "csrrs": 2, "csrrc": 3, "csrrwi": 5, "csrrsi": 6, "csrrci": 7}
FIXAS = {"ecall": 0x00000073, "ebreak": 0x00100073, "mret": 0x30200073,
         "fence": 0x0FF0000F, "fence.i": 0x0000100F}

def enc_R(f7, rs2, rs1, f3, rd, op): return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op
def enc_I(imm, rs1, f3, rd, op): return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op
def enc_S(imm, rs2, rs1, f3, op):
    return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((imm & 0x1F) << 7) | op
def enc_B(imm, rs2, rs1, f3, op):
    return (((imm >> 12) & 1) << 31) | (((imm >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) \
        | (f3 << 12) | (((imm >> 1) & 0xF) << 8) | (((imm >> 11) & 1) << 7) | op
def enc_U(imm, rd, op): return ((imm & 0xFFFFF) << 12) | (rd << 7) | op
def enc_J(imm, rd, op):
    return (((imm >> 20) & 1) << 31) | (((imm >> 1) & 0x3FF) << 21) | (((imm >> 11) & 1) << 20) \
        | (((imm >> 12) & 0xFF) << 12) | (rd << 7) | op

def _reg(s):
    try:
        return REGISTRADORES[s.strip()]
    except KeyError:
        raise ValueError(f"registrador inválido: {s!r}")

def _valor(s, rotulos=None):
    s = s.strip()
    if rotulos and s in rotulos:
        return rotulos[s]
    if len(s) == 3 and s[0] == s[2] == "'":          # literal de caractere: 'A'
        return ord(s[1])
    return int(s, 0)

def _faixa(valor, bits, assinado=True):
    lim = 1 << (bits - 1)
    if not (-lim <= valor < lim if assinado else 0 <= valor < (1 << bits)):
        raise ValueError(f"imediato {valor} não cabe em {bits} bits")
    return valor

def _mem(s):
    #"imm(reg)" -> (imm, reg)
    imm, reg = s.strip().rstrip(")").split("(")
    return _valor(imm), _reg(reg)

def montar(texto, base=INICIO_PROGRAMA):
    """Retorna (lista de palavras de 32 bits, dicionário de rótulos)"""
    rotulos, itens, end = {}, [], base

    #Passada 1: expande pseudo-instruções e descobre o endereço de cada rótulo
    for linha in texto.splitlines():
        linha = linha.split("#")[0].strip()
        while ":" in linha:
            nome, linha = linha.split(":", 1)
            rotulos[nome.strip()] = end
            linha = linha.strip()
        if not linha:
            continue
        mn, _, resto = linha.partition(" ")
        ops = [o.strip() for o in resto.split(",")] if resto.strip() else []
        if mn == "nop":       reais = [("addi", ["zero", "zero", "0"])]
        elif mn == "mv":      reais = [("addi", [ops[0], ops[1], "0"])]
        elif mn == "not":     reais = [("xori", [ops[0], ops[1], "-1"])]
        elif mn == "neg":     reais = [("sub", [ops[0], "zero", ops[1]])]
        elif mn == "seqz":    reais = [("sltiu", [ops[0], ops[1], "1"])]
        elif mn == "snez":    reais = [("sltu", [ops[0], "zero", ops[1]])]
        elif mn == "j":       reais = [("jal", ["zero", ops[0]])]
        elif mn == "call":    reais = [("jal", ["ra", ops[0]])]
        elif mn == "jr":      reais = [("jalr", ["zero", f"0({ops[0]})"])]
        elif mn == "ret":     reais = [("jalr", ["zero", "0(ra)"])]
        elif mn == "beqz":    reais = [("beq", [ops[0], "zero", ops[1]])]
        elif mn == "bnez":    reais = [("bne", [ops[0], "zero", ops[1]])]
        elif mn == "csrw":    reais = [("csrrw", ["zero", ops[0], ops[1]])]
        elif mn == "csrr":    reais = [("csrrs", [ops[0], ops[1], "zero"])]
        elif mn == "li":
            v = s32(_valor(ops[1]))
            if -2048 <= v <= 2047:
                reais = [("addi", [ops[0], "zero", str(v)])]
            else:
                #LUI carrega os 20 bits de cima; +0x800 compensa o sinal do ADDI
                hi, lo = ((v + 0x800) >> 12) & 0xFFFFF, sext(v & 0xFFF, 12)
                reais = [("lui", [ops[0], str(hi)])] + ([("addi", [ops[0], ops[0], str(lo)])] if lo else [])
        else:
            reais = [(mn, ops)]
        for item in reais:
            itens.append((end, *item))
            end += 4

    #Passada 2: codifica cada instrução
    palavras = []
    for end, mn, ops in itens:
        def alvo(s):                                  # rótulo -> deslocamento relativo ao PC
            return (rotulos[s.strip()] - end) if s.strip() in rotulos else _valor(s)
        if mn in TIPO_R:
            f3, f7 = TIPO_R[mn]
            w = enc_R(f7, _reg(ops[2]), _reg(ops[1]), f3, _reg(ops[0]), 0x33)
        elif mn in TIPO_I:
            w = enc_I(_faixa(_valor(ops[2]), 12), _reg(ops[1]), TIPO_I[mn], _reg(ops[0]), 0x13)
        elif mn in TIPO_I_SHIFT:
            f3, f7 = TIPO_I_SHIFT[mn]
            w = enc_R(f7, _faixa(_valor(ops[2]), 5, False), _reg(ops[1]), f3, _reg(ops[0]), 0x13)
        elif mn in LOADS:
            imm, rs1 = _mem(ops[1])
            w = enc_I(_faixa(imm, 12), rs1, LOADS[mn], _reg(ops[0]), 0x03)
        elif mn in STORES:
            imm, rs1 = _mem(ops[1])
            w = enc_S(_faixa(imm, 12), _reg(ops[0]), rs1, STORES[mn], 0x23)
        elif mn in BRANCHES:
            w = enc_B(_faixa(alvo(ops[2]), 13), _reg(ops[1]), _reg(ops[0]), BRANCHES[mn], 0x63)
        elif mn == "jal":
            w = enc_J(_faixa(alvo(ops[1]), 21), _reg(ops[0]), 0x6F)
        elif mn == "jalr":
            imm, rs1 = _mem(ops[1])
            w = enc_I(_faixa(imm, 12), rs1, 0, _reg(ops[0]), 0x67)
        elif mn == "lui":
            w = enc_U(_valor(ops[1]), _reg(ops[0]), 0x37)
        elif mn == "auipc":
            w = enc_U(_valor(ops[1]), _reg(ops[0]), 0x17)
        elif mn in FIXAS:
            w = FIXAS[mn]
        elif mn in CSR_OPS:
            csr = CSR_NOMES[ops[1]] if ops[1] in CSR_NOMES else _valor(ops[1])
            f3 = CSR_OPS[mn]
            origem = _reg(ops[2]) if f3 < 4 else _faixa(_valor(ops[2]), 5, False)
            w = enc_I(csr, origem, f3, _reg(ops[0]), 0x73)
        elif mn == ".word":
            w = u32(_valor(ops[0], rotulos))
        else:
            raise ValueError(f"instrução desconhecida: {mn}")
        palavras.append(u32(w))
    return palavras, rotulos


#------------------------------------------------------------------------------
# 6.) PROGRAMA DE TESTE
#------------------------------------------------------------------------------
#Cada bloco confere o resultado esperado; qualquer divergência salta para "falha".
PROGRAMA_TESTE = """
inicio:
    lui   sp, 0x80                # sp = 0x80000 (topo da RAM)

    # ---------- 1) SHIFTS, LOGICAS E COMPARACAO ----------
    addi  t0, zero, -16           # t0 = 0xFFFFFFF0
    addi  t2, zero, 2
    addi  t3, zero, 1
    srai  t1, t0, 2               # -4  (preserva o sinal)
    li    t6, -4
    bne   t1, t6, falha
    srli  t1, t0, 28              # 0xF (preenche com zeros)
    li    t6, 15
    bne   t1, t6, falha
    slli  t1, t0, 1               # -32
    li    t6, -32
    bne   t1, t6, falha
    sll   t1, t0, t2              # -64
    li    t6, -64
    bne   t1, t6, falha
    sra   t1, t0, t2              # -4
    li    t6, -4
    bne   t1, t6, falha
    srl   t1, t0, t2              # 0x3FFFFFFC
    li    t6, 0x3FFFFFFC
    bne   t1, t6, falha
    slt   t1, t0, t3              # -16 < 1 com sinal = 1
    bne   t1, t3, falha
    sltu  t1, t0, t3              # 0xFFFFFFF0 < 1 sem sinal = 0
    bne   t1, zero, falha
    slti  t1, t0, -15             # 1
    bne   t1, t3, falha
    sltiu t1, t0, -15             # 1
    bne   t1, t3, falha
    xor   t1, t0, t3              # -15
    li    t6, -15
    bne   t1, t6, falha
    or    t1, t0, t3              # -15
    bne   t1, t6, falha
    and   t1, t0, t3              # 0
    bne   t1, zero, falha
    xori  t1, t0, 15              # -1
    li    t6, -1
    bne   t1, t6, falha
    ori   t1, t0, 5               # -11
    li    t6, -11
    bne   t1, t6, falha
    andi  t1, t0, 60              # 0x30 = 48
    li    t6, 48
    bne   t1, t6, falha
    sub   t1, t0, t3              # -17
    li    t6, -17
    bne   t1, t6, falha

    # ---------- 2) LUI, AUIPC, JAL, JALR ----------
    lui   t0, 0x12345             # t0 = 0x12345000
    srli  t1, t0, 12
    li    t6, 0x12345
    bne   t1, t6, falha
    auipc t0, 0                   # t0 = PC desta instrucao
    jal   t1, proximo             # t1 = PC + 4 da instrucao jal (= t0 + 8)
proximo:
    sub   t2, t1, t0
    addi  t3, zero, 8
    bne   t2, t3, falha
    auipc t0, 0                   # P
    addi  t0, t0, 16              # alvo = P + 16
    jalr  t1, 0(t0)               # salta para P + 16 e guarda P + 12 em t1
    j     falha                   # P + 12: nao pode ser executada
    sub   t2, t0, t1              # P + 16: alvo do jalr (t0 - t1 = 4)
    addi  t3, zero, 4
    bne   t2, t3, falha

    # ---------- 3) LOADS E STORES NA RAM ----------
    lui   s2, 2                   # s2 = 0x2000 (area de dados)
    li    t0, 0x80FF7F01          # bytes na memoria: 01 7F FF 80 (little-endian)
    sw    t0, 0(s2)
    lb    t1, 0(s2)               # 1
    li    t6, 1
    bne   t1, t6, falha
    lb    t1, 1(s2)               # 127
    li    t6, 127
    bne   t1, t6, falha
    lb    t1, 2(s2)               # 0xFF com sinal = -1
    li    t6, -1
    bne   t1, t6, falha
    lbu   t1, 2(s2)               # 255
    li    t6, 255
    bne   t1, t6, falha
    lb    t1, 3(s2)               # 0x80 com sinal = -128
    li    t6, -128
    bne   t1, t6, falha
    lbu   t1, 3(s2)               # 128
    li    t6, 128
    bne   t1, t6, falha
    lh    t1, 2(s2)               # 0x80FF com sinal = -32513
    li    t6, -32513
    bne   t1, t6, falha
    lhu   t1, 2(s2)               # 0x80FF = 33023
    li    t6, 33023
    bne   t1, t6, falha
    lw    t1, 0(s2)
    bne   t1, t0, falha
    sb    zero, 0(s2)             # 0x80FF7F00
    sh    zero, 2(s2)             # 0x00007F00
    lw    t1, 0(s2)
    li    t6, 0x7F00
    bne   t1, t6, falha

    # ---------- 4) DESVIOS ----------
    addi  t0, zero, -1            # -1 com sinal / 0xFFFFFFFF sem sinal
    addi  t1, zero, 1
    beq   t0, t1, falha           # nao salta
    bne   t0, t1, b1              # salta
    j     falha
b1: blt   t0, t1, b2              # -1 < 1
    j     falha
b2: bge   t1, t0, b3              # 1 >= -1
    j     falha
b3: bltu  t1, t0, b4              # 1 < 0xFFFFFFFF
    j     falha
b4: bgeu  t0, t1, b5              # 0xFFFFFFFF >= 1
    j     falha
b5: blt   t1, t0, falha           # as 4 condicoes abaixo sao falsas
    bltu  t0, t1, falha
    bge   t0, t1, falha
    bgeu  t1, t0, falha
    beq   t0, t0, b6
    j     falha
b6:

    # ---------- 5) LACOS E CHAMADA DE FUNCAO ----------
    addi  s0, zero, 0
    addi  t0, zero, 1
    addi  t1, zero, 11
soma:
    add   s0, s0, t0
    addi  t0, t0, 1
    blt   t0, t1, soma            # s0 = 1 + 2 + ... + 10 = 55
    addi  a0, zero, 5
    call  fatorial                # a0 = 5! = 120
    mv    s1, a0
    li    t6, 55
    bne   s0, t6, falha
    li    t6, 120
    bne   s1, t6, falha

    # ---------- 6) E/S PROGRAMADA: ESCREVE NA VRAM COM SW ----------
    lui   s3, 0x80                # s3 = 0x80000 (inicio da VRAM)
    li    t0, 0x414D4F53          # "SOMA"
    sw    t0, 0(s3)
    li    t0, '='
    sb    t0, 4(s3)
    mv    a0, s0
    addi  a1, s3, 5
    call  imprime_decimal         # "55"
    li    t0, 0x3D544146          # "FAT="
    sw    t0, 32(s3)              # linha 1 da VRAM
    mv    a0, s1
    addi  a1, s3, 36
    call  imprime_decimal         # "120"
    li    t0, 0x32335652          # "RV32"
    sw    t0, 64(s3)              # linha 2 da VRAM
    li    t0, 0x4B4F2049          # "I OK"
    sw    t0, 68(s3)

    # ---------- 7) E/S MAPEADA: TERMINAL ----------
    li    t0, 0x9FC00             # TERMINAL_TX
    li    t1, 'O'
    sb    t1, 0(t0)
    li    t1, 'K'
    sb    t1, 0(t0)
    li    t1, 10                  # nova linha
    sb    t1, 0(t0)

    # ---------- 8) INTERRUPCAO DE TECLADO ----------
    lui   t0, 2
    sw    zero, 0x100(t0)         # contador de teclas tratadas (RAM 0x2100) = 0
    csrrsi zero, mstatus, 8       # MIE = 1: habilita interrupcoes
espera:
    lui   t0, 2
    lw    t1, 0x100(t0)           # teclas ja tratadas pelo tratador
    addi  t2, zero, 2
    blt   t1, t2, espera          # espera 2 teclas
    csrrci zero, mstatus, 8       # MIE = 0
    addi  a0, zero, 0
    ecall                         # fim do programa (codigo de saida 0)

falha:
    lui   s3, 0x80
    li    t0, 0x4C494146          # "FAIL"
    sw    t0, 160(s3)             # linha 5 da VRAM
    addi  a0, zero, 1
    ecall

# a0 = n  ->  a0 = n!  (sem extensao M: multiplica somando)
fatorial:
    addi  t0, zero, 1             # resultado
    mv    t1, a0                  # contador
fat_loop:
    beqz  t1, fat_fim
    addi  t2, zero, 0             # t2 = resultado * contador
    mv    t3, t0
    mv    t4, t1
mul_loop:
    beqz  t4, mul_fim
    add   t2, t2, t3
    addi  t4, t4, -1
    j     mul_loop
mul_fim:
    mv    t0, t2
    addi  t1, t1, -1
    j     fat_loop
fat_fim:
    mv    a0, t0
    ret

# a0 = valor, a1 = endereco destino -> escreve os digitos decimais em ASCII
imprime_decimal:
    addi  sp, sp, -16
    sw    ra, 12(sp)
    addi  t0, zero, 0             # quantidade de digitos
    addi  t1, zero, 10
dec_loop:
    mv    t2, a0                  # t2 = resto
    addi  t3, zero, 0             # t3 = quociente
dec_sub:
    blt   t2, t1, dec_fim
    sub   t2, t2, t1
    addi  t3, t3, 1
    j     dec_sub
dec_fim:
    addi  t2, t2, 48              # '0' + resto
    add   t4, sp, t0
    sb    t2, 0(t4)               # empilha o digito
    addi  t0, t0, 1
    mv    a0, t3
    bnez  a0, dec_loop
dec_pop:
    addi  t0, t0, -1
    add   t4, sp, t0
    lbu   t2, 0(t4)
    sb    t2, 0(a1)               # desempilha para o destino
    addi  a1, a1, 1
    bnez  t0, dec_pop
    lw    ra, 12(sp)
    addi  sp, sp, 16
    ret
"""

#Tratador de interrupção (fica no trecho de RAM apontado por mtvec: 0x100)
TRATADOR_INTERRUPCAO = """
tratador:
    addi  sp, sp, -16             # salva os registradores que serao usados
    sw    t4, 0(sp)
    sw    t5, 4(sp)
    sw    t6, 8(sp)
    li    t6, 0x9FC04             # TECLADO_DADO
    lw    t5, 0(t6)               # le a tecla (limpa a requisicao)
    li    t6, 0x2100              # contador de teclas tratadas
    lw    t4, 0(t6)
    li    t6, 0x80060             # linha 3 da VRAM
    add   t6, t6, t4
    sb    t5, 0(t6)               # escreve a tecla na VRAM
    addi  t4, t4, 1
    li    t6, 0x2100
    sw    t4, 0(t6)
    lw    t4, 0(sp)
    lw    t5, 4(sp)
    lw    t6, 8(sp)
    addi  sp, sp, 16
    mret
"""

def novo_computador(programa=PROGRAMA_TESTE):
    #Monta o computador: barramento (com RAM, VRAM e E/S) + CPU
    bus = Barramento()
    bus.carregar(VETOR_INTERRUPCAO, montar(TRATADOR_INTERRUPCAO, VETOR_INTERRUPCAO)[0])
    palavras, rotulos = montar(programa, INICIO_PROGRAMA)
    bus.carregar(INICIO_PROGRAMA, palavras)
    return CPU(bus), rotulos


def autoteste():
    import random
    random.seed(404)
    M = 0xFFFFFFFF
    sg = lambda x: x - (1 << 32) if x & 0x80000000 else x

    def executa(palavra, regs=None, pc=INICIO_PROGRAMA):
        cpu = CPU(Barramento())
        cpu.pc.value = pc
        cpu.bus.write32(pc, palavra)
        for i, v in (regs or {}).items():
            cpu.regs[i] = v & M
        cpu.step()
        return cpu

    #A) Encodings conhecidos da especificação RISC-V (validam o montador)
    conhecidos = {
        "addi x1, x0, 1": 0x00100093, "add x3, x1, x2": 0x002081B3, "sub x3, x1, x2": 0x402081B3,
        "lui x5, 0x12345": 0x123452B7, "jal x0, 0": 0x0000006F, "ret": 0x00008067,
        "ecall": 0x00000073, "ebreak": 0x00100073, "beq x0, x0, -4": 0xFE000EE3,
        "sw x5, 8(x2)": 0x00512423, "lw x6, 4(x2)": 0x00412303,
        "slli x1, x1, 2": 0x00209093, "srai x1, x1, 3": 0x4030D093,
    }
    for texto, esperado in conhecidos.items():
        obtido = montar(texto, 0)[0][0]
        assert obtido == esperado, f"{texto}: 0x{obtido:08X} != 0x{esperado:08X}"

    #B) Aritméticas/lógicas (R e I) contra um modelo de referência independente
    ref = {"add": lambda a, b: (a + b) & M, "sub": lambda a, b: (a - b) & M,
           "sll": lambda a, b: (a << (b & 31)) & M, "slt": lambda a, b: int(sg(a) < sg(b)),
           "sltu": lambda a, b: int(a < b), "xor": lambda a, b: a ^ b,
           "srl": lambda a, b: a >> (b & 31), "sra": lambda a, b: (sg(a) >> (b & 31)) & M,
           "or": lambda a, b: a | b, "and": lambda a, b: a & b}
    ref_i = {"addi": "add", "slti": "slt", "sltiu": "sltu", "xori": "xor", "ori": "or", "andi": "and",
             "slli": "sll", "srli": "srl", "srai": "sra"}
    casos = [0, 1, M, 0x80000000, 0x7FFFFFFF] + [random.getrandbits(32) for _ in range(40)]
    for a in casos:
        for b in casos[:12]:
            for mn, f in ref.items():
                cpu = executa(montar(f"{mn} x3, x1, x2", 0)[0][0], {1: a, 2: b})
                assert cpu.regs[3] == f(a, b), (mn, hex(a), hex(b))
        for mn, base in ref_i.items():
            for _ in range(8):
                imm = random.randint(0, 31) if mn in TIPO_I_SHIFT else random.randint(-2048, 2047)
                cpu = executa(montar(f"{mn} x3, x1, {imm}", 0)[0][0], {1: a})
                assert cpu.regs[3] == ref[base](a, imm & M), (mn, hex(a), imm)

    #C) Desvios: condição verdadeira -> PC + imm; falsa -> PC + 4
    cond = {"beq": lambda a, b: a == b, "bne": lambda a, b: a != b, "blt": lambda a, b: sg(a) < sg(b),
            "bge": lambda a, b: sg(a) >= sg(b), "bltu": lambda a, b: a < b, "bgeu": lambda a, b: a >= b}
    for mn, f in cond.items():
        for a in casos[:10]:
            for b in casos[:10]:
                off = random.choice([-4096, -8, 8, 2046, 4094])
                cpu = executa(montar(f"{mn} x1, x2, {off}", 0)[0][0], {1: a, 2: b}, pc=0x2000)
                assert cpu.pc.value == (0x2000 + (off if f(a, b) else 4)), (mn, hex(a), hex(b), off)

    #D) JAL, JALR, LUI, AUIPC, x0 fixo
    for off in (-1048576, -262144, -2, 2, 262144, 1048574):          # extremos do imediato J (21 bits)
        cpu = executa(montar(f"jal x1, {off}", 0)[0][0], pc=0x70000)
        assert cpu.pc.value == u32(0x70000 + off) and cpu.regs[1] == 0x70004, off
    cpu = executa(montar("jalr x1, 5(x2)", 0)[0][0], {2: 0x1000})
    assert cpu.pc.value == 0x1004 and cpu.regs[1] == INICIO_PROGRAMA + 4     # bit 0 zerado
    cpu = executa(montar("jalr x2, 4(x2)", 0)[0][0], {2: 0x1000})
    assert cpu.pc.value == 0x1004 and cpu.regs[2] == INICIO_PROGRAMA + 4     # rd == rs1
    cpu = executa(montar("lui x1, 0xFFFFF", 0)[0][0])
    assert cpu.regs[1] == 0xFFFFF000
    cpu = executa(montar("auipc x1, 0xFFFFF", 0)[0][0], pc=0x2000)
    assert cpu.regs[1] == u32(0x2000 + 0xFFFFF000)
    cpu = executa(montar("addi x0, x0, 5", 0)[0][0])
    assert cpu.regs[0] == 0

    #E) Loads e stores com extensão de sinal
    cpu = CPU(Barramento())
    cpu.bus.write32(0x3000, 0x80FF7F01)
    for mn, off, esperado in [("lb", 3, M - 127), ("lbu", 3, 0x80), ("lh", 2, u32(-32513)),
                              ("lhu", 2, 0x80FF), ("lw", 0, 0x80FF7F01), ("lb", 1, 0x7F)]:
        cpu.regs[1], cpu.pc.value = 0x3000, INICIO_PROGRAMA
        cpu.bus.write32(INICIO_PROGRAMA, montar(f"{mn} x3, {off}(x1)", 0)[0][0])
        cpu.step()
        assert cpu.regs[3] == esperado, (mn, hex(cpu.regs[3]))
    cpu = executa(montar("sw x2, -4(x1)", 0)[0][0], {1: 0x3004, 2: 0xCAFEBABE})
    assert cpu.bus.read32(0x3000) == 0xCAFEBABE

    #F) Barramento: mapa de memória e proteção
    bus = Barramento()
    bus.write32(0x7FFFC, 1)
    bus.write8(0x80000, 65)
    assert bus.read8(0x80000) == 65 and bus.vram.matriz[0][0] == 65
    for end in (0x90000, 0x9FBFF, 0xA0000):
        try:
            bus.read8(end)
            assert False, "deveria gerar BusError"
        except BusError:
            pass

    #G) Instrução ilegal
    try:
        executa(0xFFFFFFFF)
        assert False
    except InstrucaoIlegal:
        pass

    #H) Programa de teste completo (com 2 teclas pressionadas durante a execução)
    cpu, _ = novo_computador()
    cpu.run(max_instrucoes=20000, eventos={150: lambda c: c.bus.io.pressionar("K"),
                                           155: lambda c: c.bus.io.pressionar("Y")})
    vram = lambda l: "".join(chr(b) if b else " " for b in cpu.bus.vram.matriz[l]).rstrip()
    assert cpu.parada and cpu.codigo_saida == 0, "o programa de teste sinalizou falha"
    assert vram(0) == "SOMA=55" and vram(1) == "FAT=120" and vram(2) == "RV32I OK" and vram(3) == "KY"
    print("autoteste: todos os testes passaram")


if __name__ == "__main__":
    autoteste()
    print("\n=== PROGRAMA DE TESTE ===")
    cpu, rotulos = novo_computador()
    #Os eventos simulam o teclado: a tecla é pressionada no ciclo indicado (fila + IRQ).
    #As interrupções são habilitadas por volta do ciclo 455, então as teclas chegam durante a espera
    cpu.run(max_instrucoes=20000, n_video=250,
            eventos={470: lambda c: c.bus.io.pressionar("K"),
                     500: lambda c: c.bus.io.pressionar("Y")})
    cpu.bus.vram.exibir(titulo="VRAM FINAL")
    cpu.dump()
