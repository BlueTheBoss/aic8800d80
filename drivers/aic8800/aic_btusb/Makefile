ifneq ($(KERNELRELEASE),)

obj-m += aic_btusb.o

# Stack selection (see aic_btusb.h): the desktop tree targets the kernel
# BlueZ stack, so CONFIG_AICBT_BLUEDROID is left undefined (CONFIG_BLUEDROID
# becomes 0). Android builds pass CONFIG_AICBT_BLUEDROID=1.
ccflags-$(CONFIG_PLATFORM_UBUNTU) += -DCONFIG_PLATFORM_UBUNTU

else

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build

.PHONY: all clean

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

endif
