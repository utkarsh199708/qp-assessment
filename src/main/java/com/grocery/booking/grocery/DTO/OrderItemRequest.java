package com.grocery.booking.grocery.DTO;

import lombok.Data;

@Data
public class OrderItemRequest {
    private Long itemId;
    private int quantity;

    // getters and setters
}
